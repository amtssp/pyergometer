#!/bin/bash
# Fetches the newest pyergometer from GitHub and restarts it. Run it from the
# project folder on the Raspberry Pi:
#
#     ./update.sh            (refuses while a workout is running)
#     ./update.sh --force    (updates anyway; stops a running workout)
#
# Profiles, saved workouts, bike settings and logs are not touched: they are
# not part of the Git repository.
set -euo pipefail

SERVICE=pyergometer
PORT=8765
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if grep -q ' / overlay ' /proc/mounts 2>/dev/null; then
    echo "Skrivebeskyttelse (Overlay File System) er slået til, så en opdatering" >&2
    echo "ville forsvinde ved næste genstart. Slå den fra i raspi-config først." >&2
    exit 1
fi

if [ "${1:-}" != "--force" ] && \
        curl -fs "http://127.0.0.1:$PORT/api/status" | grep -q '"running": true'; then
    echo "Der kører en træning. Vent til den er færdig, eller brug: ./update.sh --force" >&2
    exit 1
fi

echo "==> Henter nyeste version"
before="$(git rev-parse HEAD)"
git pull --ff-only
after="$(git rev-parse HEAD)"

if [ "$before" = "$after" ]; then
    echo "Allerede den nyeste version ($(git log -1 --format='%h %s'))."
else
    echo "Opdateret til: $(git log -1 --format='%h %s')"
    if ! git diff --quiet "$before" "$after" -- requirements.txt; then
        echo "==> Installerer nye Python-pakker"
        "$DIR/.venv/bin/pip" install -r requirements.txt
    fi
fi

echo "==> Genstarter $SERVICE"
sudo systemctl restart "$SERVICE"
if systemctl cat "$SERVICE-wifi-watchdog" > /dev/null 2>&1; then
    sudo systemctl restart "$SERVICE-wifi-watchdog" || true
fi
for attempt in $(seq 1 20); do
    if curl -fs -o /dev/null "http://127.0.0.1:$PORT/"; then
        echo "Pyergometer kører igen på http://$(hostname).local:$PORT/"
        exit 0
    fi
    sleep 1
done

echo "Siden svarer ikke. Se fejlen med: journalctl -u $SERVICE -n 50" >&2
exit 1
