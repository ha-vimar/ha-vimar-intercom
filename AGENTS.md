# AGENTS.md

Guidance for coding agents (Claude Code reads this file through the `CLAUDE.md` symlink).

## What this is

A HACS custom integration (`custom_components/vimar_intercom`, domain `vimar_intercom`) for
Vimar/Elvox video intercoms. It emulates the official VIEW app with its own asyncio SIP stack,
registering over the Vimar cloud TLS relay or over local UDP to the intercom. Media is RTP or
SRTP, H.264 video plus G.711 audio. It exposes entities, services, ring photos and clips, the
`/av` MPEG-TS stream for HA's camera, and a Lovelace card (`www/`: ES modules, entry `vimar-intercom-card.js`).

Read before changing behaviour: `README.md` (user docs; `README.it.md` is the Italian copy),
`docs/HARDWARE.md` (what real plants do), `docs/TEST_PLAN.md` (field tests),
`CONTRIBUTING.md` (ground rules, PR checklist). `custom_components/vimar_intercom/ARCHITECTURE.md`
is older and partly stale (Italian); trust the code over it.

## Architecture map

All in `custom_components/vimar_intercom/`.

- `sip_client.py`: the SIP stack. Transport (UDP, TLS with fallback), auth retries, REGISTER,
  INVITE/answer, MESSAGE, INFO (`picture_fast_update` keyframe requests), dialogs.
  State is module-level globals, not a class.
- `sdp.py`: the SDP we offer or answer (`build_sdp`) and the parser for the peer's
  (`parse_sdp`), plus the local SRTP keys our last SDP advertised.
- `sip_message.py`: the SIP message layer. `_parse` (first line, headers, body), header
  helpers, TLS stream framing (`_split_stream`, `MAX_SIP_BODY`) and the Digest response
  (`_make_auth`). No sockets, no call state.
- `hub.py`: `VimarIntercomHub`, the orchestrator. Call lifecycle, ring/state/event callbacks
  for entities, stats, keepalive, keyframe requests (a burst at call start, one per lost
  packet repeated each second until the IDR, never periodic, never in the ring preview),
  camera target learning, device inventory. Two mixins it inherits:
  `plant_messages.py` (`PlantMessages`) parses the SIP MESSAGEs the plant sends (status,
  phonebook, notifications) and `ring_media.py` (`RingMedia`) saves the ring photo, clip and log.
- `media_handler.py`: RTP/SRTP transports, STUN, G.711 codec, H.264 depacketising, talk
  queue, PCM taps, audio WebSocket broadcast. `srtp.py` is the AES-CM crypto, `rtcp.py` a
  debug-only RTCP probe.
- `av_stream.py`: `/av`, the call's RTP through one ffmpeg into MPEG-TS, fanned out to every
  subscriber (HA stream worker, go2rtc). `av_passive.py`: `/av?autocall=0&idle=image`, a
  continuous re-encoded stream (standby image when idle) for Scrypted/go2rtc/Frigate.
- `frame_grabber.py`: per-call ffmpeg keeping the latest JPEG, plus the MP4 ring clip.
  `ring_log.py` stores ring photos/clips and the log the card reads.
- `__init__.py`: entry setup/teardown, `www/` served as a folder for the card (plain and
  under a version segment, so a change to any card module reaches browsers). `services.py`: the
  `vimar_intercom.*` services. `views.py`: the HTTP views (`/api/vimar_intercom/av`,
  `audio_ws`, `debug`, `rings`).
- `config_flow.py`: config flow (QR, manual, zeroconf `_eipvdes._tcp`) with transport probe.
  `options_flow.py`: the options flow (network, settings, HomeKit, /av key, phonebook
  fetch/import from intercom or cloud).
- `runtime.py`: module-wide settings (`R.*`) filled by `configure(entry.data)` at setup;
  `plant_state.py` holds what the plant tells us at runtime (detected model, media encryption);
  `const.py` holds only static, non-plant constants.
- `profiles.py`: per-plant-family defaults (transport, media encryption). A starting point;
  the setup probe wins.
- Pure modules, no HA/aiohttp imports (list `PURE` in `tests/test_smoke.py`): `const`,
  `runtime`, `plant_state`, `qr_decoder`, `discovery`, `rest_client`, `cloud_phonebook`, `rubrica_import`,
  `validate`, `log_redact`, `model_detect`, `srtp`. Keep them that way.
- Entity platforms: `camera`, `event`, `lock`, `button`, `switch`, `sensor`, `binary_sensor`,
  `select`, `text`. Away message: `away_config.py`, `away_tts.py`. Ring webhook: `webhook.py`.

Only one config entry is allowed, because SIP and media state are module-wide. The config
flow enforces it (`_has_entry`, abort `single_instance_allowed`), not `single_config_entry` in
the manifest: that flag stops every new flow, mDNS discovery included, and a Tab that changes
IP in local UDP was no longer followed. Do not design for two entries.

## Tests

Tests run without Home Assistant installed: `tests/conftest.py` stubs the HA modules and the
package `__init__`. `tests/harness/` is a fake panel on 127.0.0.1 for the SIP end-to-end tests.

```bash
pip install -r requirements-dev.txt
python -m pytest tests                     # default run, without the slow e2e tests (~1 min)
python -m pytest tests -m slow             # the slow e2e tests (~2 min)
python -m pytest tests -m "not live and not media and not browser"   # both, what CI runs
python -m pytest tests/test_srtp.py        # one file
python -m pytest tests -k manifest         # by name
python -m pytest tests -m media            # real ffmpeg/aiohttp media (needs ffmpeg, aiohttp)
python -m pytest tests -m browser          # the card in Chromium/WebKit (needs playwright, aiohttp)
.claude/skills/card-browser-tests/run.sh   # the same, in a disposable Playwright container
```

`live`, `media` and `browser` are excluded by default (`addopts` in `pyproject.toml`). Missing
tools make `media`/`browser` tests skip. `live` needs the real intercom: see the safety rules.

### Coverage rule (mandatory, enforced in CI)

Line plus branch coverage of `custom_components/vimar_intercom` must stay at or above **95%**
on the default test run. The threshold (`fail_under`) lives in `pyproject.toml`.

```bash
python -m pytest --cov=custom_components/vimar_intercom --cov-branch --cov-report=term-missing
```

- Every behaviour change or bug fix comes with a test that fails without it.
- `# pragma: no cover` only for lines that really cannot run in tests, each with a comment
  giving the reason.

### Lint (enforced in CI)

`ruff check .` must be clean on the whole repo, the integration included (rules in
`pyproject.toml`; the CI job pins the ruff version). A `# noqa` needs the rule code and, when
it is not obvious, a comment with the reason.

## Conventions

- English for new code comments, docstrings, docs, commit messages and PR text. Existing
  Italian comments may stay. `README.it.md` stays Italian and is updated alongside `README.md`.
- UI strings in sync across `strings.json`, `translations/en.json`, `translations/it.json`.
- A new entity (by its unique id key) or action needs a row in `docs/ENTITIES.md` and `.it.md`:
  `tests/test_entities_doc.py` fails otherwise.
- PRs do not edit `CHANGELOG.md`: it is written at release time from the PR descriptions. Every PR has
  one or more `Changelog: <section> - <what the user sees>` lines (Enhancements, Bug fixes,
  Documentation, Security, Other changes) or `Changelog: none`, and a `Before you update: ...` line
  when users must act after updating. `.github/pull_request_template.md` shows the format;
  `tools/pr_changelog.py` checks it in CI (workflow "PR text").
- `manifest.json` keys: `domain`, `name`, then alphabetical (hassfest rule, checked by
  `tests/test_manifest_order.py`). The only manifest requirement is `pycryptodome`
  (`requests` is used too, but ships with Home Assistant and hassfest rejects it in the manifest);
  no external SIP library.
- One logical change per commit. Subject in the `type(scope): summary` form seen in history
  (`fix(sip): ...`, `docs(hardware): ...`); the body explains why.
- Logging: `logging.getLogger(__name__)`, SIP traffic at DEBUG, never `print`.
  `log_buffer.py` feeds `/api/vimar_intercom/debug` with everything, DEBUG included, through
  `log_redact.redact()`.
- Keep `/av` restricted to local requests and `/audio_ws` authenticated.

## Safety rules for agents

- Never install anything on the host machine: no system packages, no global `pip install`,
  no browsers downloaded into `~/.cache`. Dependencies go in a virtualenv or a disposable
  Docker container (`docker run --rm`). The card's browser tests have a ready-made runner:
  `.claude/skills/card-browser-tests/run.sh`.
- Never open the gate or door, never place calls or open views on a real intercom, never run
  `live` tests, unless the owner explicitly asks for that specific action.
- Never guess SIP commands or actuator tokens: a wrong one can physically open a door.
- Never put plant identifiers in code, tests, docs, commits or logs: IP addresses, cloud
  domains, MAC addresses, device IDs, IMEI, SIP users, real phone numbers, QR payloads.
  Plant values come from the config entry (`tests/test_no_hardcoded_plant_values.py` guards
  some of them). Use obvious placeholders in fixtures.
- Credentials, digest responses, tokens and SRTP keys stay masked in logs. If you add a new
  secret-bearing field, extend `log_redact.py` and its tests.

## Hardware facts that bite

Details and measurements in `docs/HARDWARE.md`.

- Media encryption mirrors the offer per m-line: when answering, each m-line uses the
  profile and crypto suite the offer used. The plant setting applies only to our own offers.
- Answer exactly the offered m-lines. Audio-only offers get audio-only answers; a declined
  video line (port 0) stays declined.
- Keyframe requests (`picture_fast_update`) are honoured by some panels (Tab 5S Up 40515)
  and ignored by others (Tab 7S Up 40517). Both send a keyframe every 3 s on their own.
- The cloud relay loses packets before they reach Home Assistant: 2 to 4 in a hundred,
  measured on a 40517 in September 2026. Video and photos must survive a lost packet
  (see `docs/TEST_PLAN.md` section 2).
- SIP addresses (entrance panels, SGA, monitors) differ per installation: options or
  phonebook, never constants.
- Verify the transport, do not infer it from `planttype`.
