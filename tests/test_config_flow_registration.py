"""The registration tests the config flow runs before creating an entry: local
UDP and cloud TLS, against fake intercoms on 127.0.0.1 (no real network)."""
from __future__ import annotations

import asyncio
import datetime
import importlib
import socket
import ssl
import threading
import time

import pytest


@pytest.fixture(scope="module")
def cf():
    # conftest stubs the HA modules config_flow imports.
    return importlib.import_module("custom_components.vimar_intercom.config_flow")


CHALLENGE = ('SIP/2.0 401 Unauthorized\r\n'
             'WWW-Authenticate: Digest realm="r", nonce="n", qop="auth", opaque="o"\r\n\r\n')


# ─── Local UDP ───────────────────────────────────────────────────────────────

def udp_intercom(replies):
    """UDP fake: for each REGISTER received, sends the next list of replies
    (an empty list: stays silent). Returns (port, requests seen)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(5)
    seen = []

    def serve():
        try:
            for answers in replies:
                data, addr = sock.recvfrom(65535)
                seen.append(data.decode())
                for answer in answers:
                    sock.sendto(answer.encode(), addr)
        except OSError:
            pass
        finally:
            sock.close()

    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1], seen


def register_udp(cf, monkeypatch, port, **kw):
    monkeypatch.setattr(cf, "DEFAULT_LOCAL_SIP_PORT", port)
    args = dict(sip_user="60999", sip_password="pw", sip_domain="example.test",
                local_proxy="127.0.0.1", local_udp_port=0, timeout=2)
    args.update(kw)
    return asyncio.run(cf._test_sip_registration(**args))


def test_an_open_intercom_registers_without_auth_and_unregisters(cf, monkeypatch):
    # The unregister gets no answer: the test result stands anyway.
    port, seen = udp_intercom([["SIP/2.0 200 OK\r\n\r\n"], []])
    ok, msg = register_udp(cf, monkeypatch, port, unregister=True, timeout=0.5)
    assert ok and msg == "Registration succeeded (no auth)"
    assert "Expires: 0\r\n" in seen[1]
    # No identity given: no instance id and no IMEI header.
    assert "+sip.instance" not in seen[0] and "Mobile-IMEI" not in seen[0]


def test_an_unexpected_final_answer_is_reported_as_such(cf, monkeypatch):
    port, _ = udp_intercom([["SIP/2.0 486 Busy Here\r\n\r\n"]])
    ok, msg = register_udp(cf, monkeypatch, port)
    assert not ok and msg == "Unexpected response: SIP/2.0 486 Busy Here"


def test_a_challenge_without_nonce_is_refused(cf, monkeypatch):
    port, _ = udp_intercom([['SIP/2.0 401 Unauthorized\r\nWWW-Authenticate: Digest realm="r"\r\n\r\n']])
    ok, msg = register_udp(cf, monkeypatch, port)
    assert not ok and msg == "Challenge without nonce/realm"


def test_wrong_credentials_are_an_authentication_refusal(cf, monkeypatch):
    port, seen = udp_intercom([[CHALLENGE], ["SIP/2.0 403 Forbidden\r\n\r\n"]])
    ok, msg = register_udp(cf, monkeypatch, port)
    assert not ok and msg == "Authentication refused: SIP/2.0 403 Forbidden"
    assert 'qop=auth' in seen[1] and 'opaque="o"' in seen[1]


def test_a_silent_intercom_is_a_timeout_with_advice(cf, monkeypatch):
    port, _ = udp_intercom([[]])
    ok, msg = register_udp(cf, monkeypatch, port, timeout=0.3)
    assert not ok and msg.startswith("Timeout") and "same network" in msg


def test_a_deadline_already_past_is_a_timeout(cf, monkeypatch):
    port, _ = udp_intercom([["SIP/2.0 200 OK\r\n\r\n"]])
    ok, msg = register_udp(cf, monkeypatch, port, timeout=0)
    assert not ok and msg.startswith("Timeout")


def test_a_closed_port_is_a_socket_error(cf, monkeypatch):
    closed = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()  # nobody listens: the ICMP refusal surfaces on recv
    ok, msg = register_udp(cf, monkeypatch, port)
    assert not ok and msg.startswith("Socket error")


def test_a_busy_local_port_falls_back_to_any_port(cf, monkeypatch):
    busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    busy.bind(("0.0.0.0", 0))
    try:
        port, seen = udp_intercom([["SIP/2.0 200 OK\r\n\r\n"]])
        ok, _ = register_udp(cf, monkeypatch, port, local_udp_port=busy.getsockname()[1])
        assert ok
        via_port = int(seen[0].split("Via: SIP/2.0/UDP ", 1)[1].split(";", 1)[0].rsplit(":", 1)[1])
        assert via_port != busy.getsockname()[1]
    finally:
        busy.close()


# ─── Cloud TLS ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tls_cert(tmp_path_factory):
    """A self-signed certificate for "localhost", written as cert + key files."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    folder = tmp_path_factory.mktemp("tls")
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def tls_relay(tls_cert, replies):
    """TLS fake relay on 127.0.0.1: for each REGISTER, the next reply (bytes);
    None closes the connection; a list is sent piece by piece. Returns (port,
    requests seen)."""
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(*tls_cert)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    seen = []

    def serve():
        try:
            raw, _ = listener.accept()
            with server_ctx.wrap_socket(raw, server_side=True) as conn:
                conn.settimeout(5)
                buffer = b""
                for reply in replies:
                    while b"\r\n\r\n" not in buffer:
                        buffer += conn.recv(65535)
                    request, _, buffer = buffer.partition(b"\r\n\r\n")
                    seen.append(request.decode())
                    if reply is None:
                        return
                    for piece in reply if isinstance(reply, list) else [reply]:
                        conn.sendall(piece)
                        time.sleep(0.02)
        except OSError:
            pass
        finally:
            listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return listener.getsockname()[1], seen


def register_cloud(cf, monkeypatch, tls_cert, targets, **kw):
    monkeypatch.setattr(cf, "CA_PATH", tls_cert[0])
    monkeypatch.setattr(cf, "_resolve_sip_targets", lambda host, port: targets)
    args = dict(sip_user="60999", sip_password="pw", cloud_domain="plant.example.test",
                cloud_proxy="localhost", timeout=3)
    args.update(kw)
    return asyncio.run(cf._test_cloud_registration(**args))


def test_without_a_cloud_domain_the_cloud_is_not_tried(cf):
    ok, msg = asyncio.run(cf._test_cloud_registration("60999", "pw", ""))
    assert not ok and "pairing QR" in msg


def test_the_cloud_challenge_is_answered_with_the_paired_identity(cf, monkeypatch, tls_cert):
    # The final answer arrives in two pieces: the head is read until it is whole.
    port, seen = tls_relay(tls_cert, [
        b"SIP/2.0 100 Trying\r\n\r\n" + CHALLENGE.encode(), [b"SIP/2.0 200 OK\r\n", b"\r\n"]])
    ok, msg = register_cloud(cf, monkeypatch, tls_cert, [("127.0.0.1", port)],
                             device_imei="000000000000000", device_uuid="uuid-1", device_name="Test")
    assert ok and msg == "Cloud registration succeeded via 127.0.0.1"
    assert "Route: <sip:localhost;transport=tls;lr>" in seen[0]
    assert '+sip.instance="<urn:uuid:uuid-1>"' in seen[0] and "Mobile-IMEI: 000000000000000" in seen[0]
    assert "Authorization: Digest" in seen[1] and 'realm="r"' in seen[1]


def test_an_open_relay_registers_at_once(cf, monkeypatch, tls_cert):
    port, seen = tls_relay(tls_cert, [b"SIP/2.0 200 OK\r\n\r\n"])
    ok, _ = register_cloud(cf, monkeypatch, tls_cert, [("127.0.0.1", port)])
    assert ok and len(seen) == 1
    assert "+sip.instance" not in seen[0] and "Mobile-IMEI" not in seen[0]


@pytest.mark.parametrize("replies, expected", [
    ([b"SIP/2.0 403 Forbidden\r\n\r\n"], "The relay refused the registration: SIP/2.0 403 Forbidden"),
    ([b'SIP/2.0 407 Proxy Auth\r\nProxy-Authenticate: Digest nonce="n"\r\n\r\n'],
     "Cloud challenge without nonce/realm"),
    ([CHALLENGE.encode(), b"SIP/2.0 403 Forbidden\r\n\r\n"],
     "Cloud authentication refused: SIP/2.0 403 Forbidden"),
])
def test_cloud_refusals_are_explained(cf, monkeypatch, tls_cert, replies, expected):
    port, _ = tls_relay(tls_cert, replies)
    ok, msg = register_cloud(cf, monkeypatch, tls_cert, [("127.0.0.1", port)])
    assert (ok, msg) == (False, expected)


def test_an_unreachable_relay_is_skipped_for_the_next_one(cf, monkeypatch, tls_cert):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    dead_port = closed.getsockname()[1]
    closed.close()
    port, _ = tls_relay(tls_cert, [b"SIP/2.0 200 OK\r\n\r\n"])
    ok, _ = register_cloud(cf, monkeypatch, tls_cert,
                           [("127.0.0.1", dead_port), ("127.0.0.1", port)])
    assert ok


def test_a_relay_closing_the_connection_fails_with_the_reason(cf, monkeypatch, tls_cert):
    port, _ = tls_relay(tls_cert, [None])
    ok, msg = register_cloud(cf, monkeypatch, tls_cert, [("127.0.0.1", port)])
    assert not ok and msg.startswith("Cloud registration failed (127.0.0.1:")


def test_an_untrusted_certificate_is_refused(cf, monkeypatch, tls_cert):
    # Without the test CA (CA_PATH missing) the self-signed relay is not trusted.
    port, _ = tls_relay(tls_cert, [b"SIP/2.0 200 OK\r\n\r\n"])
    monkeypatch.setattr(cf, "_resolve_sip_targets", lambda host, p: [("127.0.0.1", port)])
    monkeypatch.setattr(cf, "CA_PATH", "/nonexistent/ca.pem")
    ok, msg = asyncio.run(cf._test_cloud_registration(
        "60999", "pw", "plant.example.test", cloud_proxy="localhost", timeout=3))
    assert not ok and "CERTIFICATE_VERIFY_FAILED" in msg


def test_no_proxy_address_at_all_fails_cleanly(cf, monkeypatch, tls_cert):
    ok, msg = register_cloud(cf, monkeypatch, tls_cert, [])
    assert (ok, msg) == (False, "Cloud registration failed (no proxy reachable)")
