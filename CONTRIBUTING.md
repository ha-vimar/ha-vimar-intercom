# Contributing

Thanks for taking the time to contribute. This integration was reverse-engineered
on a single system (Elvox Tab 7S 2F+ WiFi, art. 40507), so reports and patches from
other hardware are genuinely valuable — especially Tab 5S / 2FV2 / IP plants.

Who reviews, merges, tests on which intercom and releases: [GOVERNANCE.md](GOVERNANCE.md).

## Ground rules

**1. Never guess SIP commands or tokens.**
This is the single most important rule. A wrong actuator token can physically open
a door or a gate. Anything that goes into the code as an *active* command must be
either documented from a capture of the official VIEW app or extracted from it. Everything
else stays out of the code: open an issue and mark it as a hypothesis.

**2. Don't put credentials in the repo.**
No SIP passwords, QR payloads, HA1 hashes, MAC addresses or cloud tokens — not in
code, not in tests, not in issue attachments. Scrub logs before posting them
(see the redaction list in the issue template). QR payloads must never be logged
above `DEBUG` level.

**3. Keep the HTTP endpoints locked down.**
`/av` stays behind `_is_local_request()`; the `/audio_ws` WebSocket
stays `requires_auth = True`. A PR that loosens either needs a very good reason
in its description.

## Development setup

For an onboarded, isolated Home Assistant instance using the current checkout,
run `./docker/dev.sh`. See [Docker Compose development](docs/DEVELOPMENT.md).
The unit tests below still run independently of Home Assistant.

```bash
git clone https://github.com/ha-vimar/ha-vimar-intercom
cd ha-vimar-intercom
pip install -r requirements-dev.txt
```

Targets: Home Assistant **2025.10+**, Python **3.13+** (what HA 2025.10 ships; CI also runs 3.14).

Runtime dependencies are deliberately minimal: `pycryptodome` and `requests` (the latter ships
with Home Assistant, so only `pycryptodome` is listed in the manifest).
**No external SIP library** — the stack in `sip_client.py` is custom and stays
custom. Please don't add `cryptography` as a runtime requirement either; `pycryptodome`
covers what we need and HA already pins it. (`requirements-dev.txt` lists `cryptography`
only so the tests can check our crypto against a second implementation.)

## Before you open a PR

```bash
python -m pytest tests/ -q          # must be green
ruff check .                        # must be clean (CI job "ruff")
python -m py_compile custom_components/vimar_intercom/*.py
```

Tests don't need a real Home Assistant instance: the modules under test are pure
Python. If you touch `sip_client.py`, `hub.py`, `runtime.py` or `qr_decoder.py`,
add a test — those are the parts that break silently on other plants.

### Tests

- `python -m pytest` runs the unit tests, about a minute. No network, no ffmpeg, no browser.
- `python -m pytest -m slow` runs the SIP end-to-end tests (`tests/test_e2e_sip.py`, `tests/test_e2e_blind.py`,
  about two minutes): the real `sip_client`, hub, media and HTTP views against a fake panel on 127.0.0.1
  (`tests/harness/`). The default run leaves them out; CI runs them.
- `python -m pytest tests -m "not live and not media and not browser and not docker" --cov --cov-report=term-missing` is the
  default run plus the slow tests, with coverage, as CI runs it: lines and branches of
  `custom_components/vimar_intercom`, and it fails below the `fail_under` threshold in `pyproject.toml` (95%). New code comes with tests that keep it there.
- `python -m pytest -m media` adds real media (`tests/test_e2e_media.py`): H.264 from ffmpeg,
  SRTP, the real `/av` ffmpeg behind a real aiohttp server, frames counted with ffprobe. Needs
  `ffmpeg`/`ffprobe` and `aiohttp`. If the first `ffmpeg` in your PATH is a launcher that runs
  the real binary as a child (Chocolatey on Windows: `kill()` stops the launcher, the real ffmpeg
  keeps `/av` open), point `FFMPEG` at the real binaries' folder:
  `FFMPEG=C:/ProgramData/chocolatey/lib/ffmpeg/tools/ffmpeg/bin python -m pytest -m media`.
- `python -m pytest -m browser` runs the dashboard card in Chromium and WebKit
  (`tests/test_e2e_card.py`). Needs `playwright` (`playwright install chromium webkit`) and `aiohttp`.
- `python -m pytest -m "media or browser or not live"` runs everything except the `live` tests,
  which need the real panel on the network. Missing tools skip their tests instead of failing.

## PR checklist

- [ ] Tests pass, and new behaviour has a test
- [ ] The PR description has one or more `Changelog: <section> - <what the user sees>` lines
      (sections: Enhancements, Bug fixes, Documentation, Security, Other changes), or `Changelog: none`,
      plus a `Before you update: ...` line when users must act after updating. The template shows how;
      the "PR text" check reads them. **Don't edit `CHANGELOG.md`**: it is written at release time from these lines
- [ ] `manifest.json` version left alone: it changes only in `release/X.Y.Z` PRs
- [ ] UI strings kept in sync across `strings.json`, `translations/it.json` and `translations/en.json`
- [ ] A new entity or action has its row in `docs/ENTITIES.md` and `docs/ENTITIES.it.md` (`tests/test_entities_doc.py` checks it)
- [ ] No credentials, MAC addresses or raw QR payloads anywhere in the diff
- [ ] Says which hardware you tested on: model, article number, firmware, and whether local UDP or cloud TLS
- [ ] Line and branch coverage stays at or above 95% (`python -m pytest tests --cov`)
- [ ] `ruff check .` is clean
- [ ] Non-obvious design decisions explained in the PR description, or in `docs/` when they concern hardware behaviour

Small, focused PRs get merged faster than big ones. If you're planning something
large — a new transport, a rewrite of the media pipeline, cloud REST support —
open an issue first so we don't both build it.

## Logging

`_LOGGER.debug` for SIP traffic, `_LOGGER.info` for user-facing events, never
`print`. In a module, take your logger with `logging.getLogger(__name__)` and
leave handlers and levels alone.

One module breaks that rule on purpose: `log_buffer.py` attaches two handlers
to the `custom_components.vimar_intercom` logger. One fills the circular buffer
behind `/api/vimar_intercom/debug?lines=N` (administrators only) with every
line, `DEBUG` included. The other forwards to Home Assistant's log from the
level the user set under `logger:` (or with `logger.set_level`), or from
`WARNING` when nobody set one. Both pass through `log_redact.redact()`, which
masks passwords, digest responses, tokens and SRTP keys. Keep the default
quiet: the bug this replaced filled one maintainer's log with hundreds of
megabytes over six months.

## Reporting hardware compatibility

Got it working on a model that isn't the Tab 7S? Please open a
**Hardware compatibility report** issue even if everything worked — knowing what
*doesn't* need changing is as useful as knowing what does.
