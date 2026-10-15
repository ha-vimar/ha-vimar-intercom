#!/usr/bin/env bash
# Start or update the development instance using the current checkout.
set -euo pipefail
umask 077
repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
compose=(docker compose --project-directory "$repo/docker" -f "$repo/docker/docker-compose.yaml")
if [[ $# -gt 1 || "${1:-up}" != up && "${1:-up}" != reset ]]; then
    printf 'Usage: %s [up|reset]\n' "$0" >&2
    exit 2
fi
if [[ -L .dev-ha ]]; then
    printf 'Refusing a symlinked .dev-ha directory.\n' >&2
    exit 1
fi
if [[ "${1:-up}" == reset ]]; then
    "${compose[@]}" down
    if [[ -d .dev-ha ]]; then
        "${compose[@]}" run --rm --no-deps --entrypoint python seed /dev-tools/reset.py
        rmdir .dev-ha
    fi
    printf 'Development environment reset.\n'
    exit 0
fi
mkdir -p .dev-ha
chmod 700 .dev-ha
"${compose[@]}" up -d --force-recreate
if ! "${compose[@]}" wait bootstrap; then
    "${compose[@]}" logs --no-log-prefix bootstrap >&2
    exit 1
fi
"${compose[@]}" logs --no-log-prefix bootstrap
