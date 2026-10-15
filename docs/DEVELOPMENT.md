# Home Assistant development with Docker Compose

From the repository root:

```bash
./docker/dev.sh
```

Requires Docker with the Compose plugin, Bash for the launcher, and internet
access to download images on first use. No host Python environment or HACS
installation is needed. The command starts the `vimar-intercom-dev` Compose application, completes HA
onboarding, verifies login and the installed Vimar setup form, and prints the URL
and credentials. Open **http://localhost:18123/vimar-dev/development** and log in
with **`developer` / `developer`**. The stack also appears in Docker Desktop.

## Edit and update

The integration, including its frontend assets, is mounted read-only from the
current checkout. Run `./docker/dev.sh` again after Python edits or switching
branches: containers are recreated to load the new code, preserving development
settings and login. Python edits are not hot-reloaded.

From the repository root, use the file directly with Docker Compose:

```bash
docker compose -f docker/docker-compose.yaml up -d --force-recreate
docker compose -f docker/docker-compose.yaml logs bootstrap           # readiness, URL and login
docker compose -f docker/docker-compose.yaml logs -f homeassistant    # application logs
docker compose -f docker/docker-compose.yaml down                    # stop; retain development settings
```

The script combines startup, waiting for bootstrap, and showing credentials. It
reports failure if startup or bootstrap fails. For another web port:

```bash
HA_DEV_PORT=18124 ./docker/dev.sh
```

Use the same variable for subsequent Compose commands. For concurrent checkouts,
also set a distinct `COMPOSE_PROJECT_NAME` and port for each. Each checkout keeps
its own state directory.

## Files and persistent state

| Path | Purpose |
|---|---|
| `docker/docker-compose.yaml` | Pinned HA/Nginx images, startup ordering, health checks and networks |
| `docker/dev.sh` | One-command start/update and readiness output |
| `docker/seed.py` | Creates missing development configuration files |
| `docker/bootstrap.py` | Creates the owner through HA's onboarding API and checks login, setup and dashboard |
| `docker/configuration.yaml`, `docker/ui-lovelace.yaml` | Minimal HA configuration and development landing page |
| `docker/nginx.conf` | Local browser access, including WebSockets |
| `.dev-ha/` | Persistent development configuration and credentials; ignored by Git |

The fixed login is for this localhost-only development instance. Credentials are
stored in `.dev-ha/credentials.json` and printed in bootstrap logs; do not share
that directory or those logs. No production configuration is copied. Bootstrap
revokes its temporary authentication session after verification.

On Linux Docker Engine, the HA image runs as root, so files inside `.dev-ha/`
can be root-owned and unreadable by the host user. The launcher still prints the
login. To delete this development configuration and start from scratch, use:

```bash
./docker/dev.sh reset
```

Reset stops/removes this Compose project's containers and networks, then clears
only `.dev-ha/` through a one-shot, network-disabled container and removes the
empty directory. **It deletes the development login, configuration and history.**
Back up any development state you want to keep first. Use the same
`COMPOSE_PROJECT_NAME` and `HA_DEV_PORT` settings as at startup. No host `sudo`,
production configuration or other Compose project is involved. Symlinked
`.dev-ha` directories are refused.

Run `./docker/dev.sh` again to create a fresh instance. To retain state instead,
use `docker compose -f docker/docker-compose.yaml down` or stop the containers.
Templates are copied only when missing: edits to templates do not overwrite an
existing development configuration.

The pytest suite includes a Linux Docker Engine lifecycle check on disposable
GitHub Actions runners: fresh onboarding, restart persistence, deletion of
root-owned state and fresh onboarding after reset. Local unit-test runs skip
that check and never start Docker automatically.

## Network isolation and scope

Only Nginx publishes a port, bound to `127.0.0.1`. HA runs on an internal Docker
network without outbound LAN or internet access. Nginx provides browser access,
not an outbound proxy for HA. The minimal configuration omits `default_config`
and automatic discovery.

On a fresh start, bootstrap opens and cancels Vimar's initial setup form without
supplying QR data or SIP credentials. It seeds no intercom entry or automations.
An existing Vimar entry is retained and bootstrap skips starting another setup
flow; HA still performs that entry's normal startup operations within the
isolated network.

This environment supports setup/options and frontend development. It does not
simulate an intercom or verify real calls, doorbell events or media. Hardware
tests need a separately configured network and the owner's permission for the
specific actions. No LAN forwarding, host networking or experimental SIP changes
are included.

Images are pinned for repeatable onboarding. When updating the HA image, verify
both fresh onboarding and a second start with retained state: `seed.py` uses
that version's onboarding storage constants.
