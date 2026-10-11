#!/bin/bash
# Installs pyergometer on a Raspberry Pi (Raspberry Pi OS) and starts it as a
# service that comes up whenever the Pi boots. Run it from the project folder
# as your normal user (not with sudo):
#
#     ./install-pi.sh
#
# Running it again is safe; it only brings the installation up to date.
set -euo pipefail

SERVICE=pyergometer
PORT=8765
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="$(id -un)"

if [ "$(id -u)" -eq 0 ]; then
    echo "Kør scriptet som din almindelige bruger, ikke med sudo." >&2
    exit 1
fi
if [ "$(uname -s)" != "Linux" ]; then
    echo "Scriptet er lavet til Raspberry Pi OS (Linux)." >&2
    exit 1
fi

echo "==> Installerer systempakker (kræver din adgangskode)"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git bluez curl

echo "==> Opretter Python-miljø i $DIR/.venv"
if [ ! -x "$DIR/.venv/bin/python" ]; then
    python3 -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/pip" install --upgrade pip
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"

echo "==> Giver $RUN_USER adgang til seriel port og Bluetooth"
sudo usermod -aG dialout,bluetooth "$RUN_USER"
sudo rfkill unblock bluetooth || true

if [ ! -f "$DIR/bike_settings.json" ]; then
    echo "==> Sætter Kettler på /dev/ttyUSB0 som standard (kan ændres på siden)"
    printf '{\n  "device_type": "kettler",\n  "device": "/dev/ttyUSB0"\n}\n' > "$DIR/bike_settings.json"
fi

echo "==> Opretter tjenesten $SERVICE, så programmet starter med Pi'en"
sudo tee "/etc/systemd/system/$SERVICE.service" > /dev/null <<EOF
[Unit]
Description=Pyergometer workout page
Wants=network-online.target
After=network-online.target bluetooth.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$DIR/.venv/bin/python $DIR/pyergometer.py --host 0.0.0.0 --port $PORT --no-browser
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
sudo systemctl restart "$SERVICE"

echo "==> Tjekker at siden svarer"
for attempt in $(seq 1 20); do
    if curl -fs -o /dev/null "http://127.0.0.1:$PORT/"; then
        echo
        echo "Pyergometer kører. Åbn siden på telefon, tablet eller pc:"
        echo "    http://$(hostname).local:$PORT/"
        for address in $(hostname -I); do
            case "$address" in *:*) ;; *) echo "    http://$address:$PORT/" ;; esac
        done
        echo
        echo "Log ud og ind igen (eller genstart), før du bruger kommandoer mod"
        echo "seriel port eller Bluetooth i en terminal; tjenesten har allerede adgang."
        echo "Opdatering senere: ./update.sh"
        exit 0
    fi
    sleep 1
done

echo "Siden svarer ikke. Se fejlen med: journalctl -u $SERVICE -n 50" >&2
exit 1
