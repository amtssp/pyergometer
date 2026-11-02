#!/bin/bash
# Keeps the Raspberry Pi's Wi-Fi alive. Every minute it checks that the Wi-Fi
# interface is connected and can reach the router. When it has been down for a
# while it recovers step by step:
#
#   1. after RECONNECT_AFTER seconds: ask NetworkManager to connect again
#      (enough when the router or access point has restarted);
#   2. after RESET_AFTER seconds, then every RESET_REPEAT seconds: reset the
#      Wi-Fi hardware - its driver is reloaded, and a USB dongle is also
#      unplugged and replugged in software (for an adapter that has hung);
#   3. only if REBOOT_AFTER is set (seconds, 0 = never): reboot the Pi, at most
#      every REBOOT_INTERVAL seconds and never while a workout is running.
#
# With PREFERRED_BSSID set, the Pi is kept on that one access point (its BSSID
# is locked in the Wi-Fi connection) instead of roaming between access points
# with the same name. If Wi-Fi has been down for UNLOCK_AFTER seconds while
# locked, the lock is lifted so any access point can be used. The lock is put
# back once the preferred access point is seen again with at least
# RELOCK_SIGNAL % signal - never during a workout, and after a failed lock
# only after a pause that doubles each time (RELOCK_PAUSE up to 4 hours).
#
# It works with any Wi-Fi adapter: the interface, driver and USB device are
# looked up when needed. With no Wi-Fi adapter or no Wi-Fi connection set to
# connect automatically (for example a cable only), it does nothing.
# Settings can also be put in $SETTINGS (shell variable lines).
# Runs as root from the pyergometer-wifi-watchdog service; see install-pi.sh.
set -u

SETTINGS=${SETTINGS:-/etc/default/pyergometer-wifi-watchdog}
# shellcheck source=/dev/null
[ -r "$SETTINGS" ] && . "$SETTINGS"

PREFERRED_BSSID=$(echo "${PREFERRED_BSSID:-}" | tr '[:lower:]' '[:upper:]')
UNLOCK_AFTER=${UNLOCK_AFTER:-120}
RELOCK_SIGNAL=${RELOCK_SIGNAL:-40}
RELOCK_PAUSE=${RELOCK_PAUSE:-900}
# A lock counts as working once Wi-Fi has stayed up this long after it.
LOCK_PROVEN_AFTER=${LOCK_PROVEN_AFTER:-600}
CHECK_INTERVAL=${CHECK_INTERVAL:-60}
RECONNECT_AFTER=${RECONNECT_AFTER:-180}
RESET_AFTER=${RESET_AFTER:-300}
RESET_REPEAT=${RESET_REPEAT:-600}
REBOOT_AFTER=${REBOOT_AFTER:-0}
REBOOT_INTERVAL=${REBOOT_INTERVAL:-21600}
PORT=${PORT:-8765}
REBOOT_STAMP=${REBOOT_STAMP:-/var/lib/pyergometer-wifi-watchdog.last-reboot}

log() {
    echo "$*"
}

wifi_interface() {
    nmcli -t -f DEVICE,TYPE device 2>/dev/null | awk -F: '$2 == "wifi" { print $1; exit }'
}

wifi_wanted() {
    # Only look after Wi-Fi when a Wi-Fi connection is meant to come up by itself.
    nmcli -t -f TYPE,AUTOCONNECT connection show 2>/dev/null | grep -q '^802-11-wireless:yes$'
}

wifi_healthy() {
    local iface=$1 state gateway
    state=$(nmcli -t -g GENERAL.STATE device show "$iface" 2>/dev/null)
    case "$state" in 100*) ;; *) return 1 ;; esac
    gateway=$(ip -4 route show default dev "$iface" 2>/dev/null | awk '{ print $3; exit }')
    [ -n "$gateway" ] || return 1
    ping -I "$iface" -c 3 -W 2 -q "$gateway" > /dev/null 2>&1
}

workout_running() {
    curl -s -m 3 "http://127.0.0.1:$PORT/api/status" 2>/dev/null |
        grep -qE '"(running|starting)": true'
}

reset_wifi() {
    local iface=$1 device module usb
    device=$(readlink -f "/sys/class/net/$iface/device" 2>/dev/null)
    module=$(basename "$(readlink -f "/sys/class/net/$iface/device/driver/module" 2>/dev/null)")
    # A USB adapter sits below a USB device directory (the one with idVendor).
    usb=$device
    while [ -n "$usb" ] && [ "$usb" != "/" ] && [ ! -e "$usb/idVendor" ]; do
        usb=$(dirname "$usb")
    done
    if [ -z "$module" ] || [ "$module" = "." ]; then
        log "Kan ikke finde driveren for $iface"
        return 1
    fi
    # Reloading the driver is what revives a hung adapter; a USB adapter is also
    # unplugged and replugged in software in between, for a complete reset.
    log "Nulstiller WiFi på $iface (driver $module)"
    # --remove-dependents also unloads helper modules (e.g. brcmfmac_wcc on the internal Wi-Fi).
    modprobe -r --remove-dependents "$module" 2> /dev/null || modprobe -r "$module"
    sleep 2
    if [ -n "$usb" ] && [ "$usb" != "/" ] && [ -w "$usb/authorized" ]; then
        log "Nulstiller USB-enheden $(cat "$usb/idVendor"):$(cat "$usb/idProduct")"
        echo 0 > "$usb/authorized"
        sleep 2
        echo 1 > "$usb/authorized"
        sleep 2
    fi
    modprobe "$module"
    sleep 10
    nmcli device connect "$(wifi_interface)" > /dev/null 2>&1 || true
}

wifi_connection() {
    # The Wi-Fi connection that comes up by itself (the one to lock to an access point).
    nmcli -t -f NAME,TYPE,AUTOCONNECT connection show 2>/dev/null |
        awk -F: '$2 == "802-11-wireless" && $3 == "yes" { print $1; exit }'
}

locked_bssid() {
    nmcli -g 802-11-wireless.bssid connection show "$1" 2>/dev/null | tr -d '\\' | tr '[:lower:]' '[:upper:]'
}

preferred_signal() {
    # The preferred access point's signal in %, or nothing when it is not seen.
    nmcli -t -f BSSID,SIGNAL device wifi list ifname "$1" 2>/dev/null | tr -d '\\' |
        awk -F: -v wanted="$PREFERRED_BSSID" \
            'toupper($1":"$2":"$3":"$4":"$5":"$6) == wanted { print $7; exit }'
}

set_lock() {
    local conn=$1 bssid=$2
    nmcli connection modify "$conn" 802-11-wireless.bssid "$bssid" > /dev/null 2>&1 &&
        nmcli --wait 40 connection up "$conn" > /dev/null 2>&1
}

maybe_reboot() {
    local now=$1 last=0
    [ "$REBOOT_AFTER" -gt 0 ] || return 0
    [ -f "$REBOOT_STAMP" ] && last=$(cat "$REBOOT_STAMP" 2>/dev/null || echo 0)
    if [ $((now - last)) -lt "$REBOOT_INTERVAL" ]; then
        return 0
    fi
    if workout_running; then
        log "WiFi stadig nede, men en træning kører; genstarter ikke"
        return 0
    fi
    log "WiFi har været nede i over $REBOOT_AFTER sekunder; genstarter Pi'en"
    echo "$now" > "$REBOOT_STAMP"
    systemctl reboot
}

log "WiFi-vagthund startet (tjek hvert ${CHECK_INTERVAL}s, forbind igen efter ${RECONNECT_AFTER}s," \
    "nulstil efter ${RESET_AFTER}s, genstart: $([ "$REBOOT_AFTER" -gt 0 ] && echo "efter ${REBOOT_AFTER}s" || echo fra)," \
    "foretrukket adgangspunkt: ${PREFERRED_BSSID:-intet})"
down_since=0
reconnected=0
last_reset=0
# Lock bookkeeping: when it was last put on, how often in a row it failed, and
# when it may be tried again.
locked_at=0
lock_failures=0
relock_after=0
while true; do
    iface=$(wifi_interface)
    if [ -z "$iface" ] || ! wifi_wanted; then
        down_since=0
        sleep "$CHECK_INTERVAL"
        continue
    fi
    conn=$(wifi_connection)
    now=$(date +%s)
    if wifi_healthy "$iface"; then
        if [ "$down_since" -ne 0 ]; then
            log "WiFi på $iface virker igen efter $(( now - down_since )) sekunder"
        fi
        down_since=0
        reconnected=0
        last_reset=0
        if [ -n "$PREFERRED_BSSID" ] && [ -n "$conn" ]; then
            if [ -n "$(locked_bssid "$conn")" ]; then
                if [ "$locked_at" -ne 0 ] && [ $((now - locked_at)) -ge "$LOCK_PROVEN_AFTER" ]; then
                    lock_failures=0
                    locked_at=0
                fi
            elif [ "$now" -ge "$relock_after" ] && ! workout_running; then
                signal=$(preferred_signal "$iface")
                if [ -n "$signal" ] && [ "$signal" -ge "$RELOCK_SIGNAL" ]; then
                    log "Låser $iface til det foretrukne adgangspunkt $PREFERRED_BSSID (signal $signal %)"
                    set_lock "$conn" "$PREFERRED_BSSID" || log "Kunne ikke forbinde til $PREFERRED_BSSID"
                    locked_at=$(date +%s)
                fi
            fi
        fi
        sleep "$CHECK_INTERVAL"
        continue
    fi

    if [ "$down_since" -eq 0 ]; then
        down_since=$now
        log "WiFi på $iface er nede"
    fi
    down=$((now - down_since))
    if [ -n "$conn" ] && [ "$down" -ge "$UNLOCK_AFTER" ] && [ -n "$(locked_bssid "$conn")" ]; then
        # The locked access point does not work: let the Pi use any access point.
        lock_failures=$((lock_failures + 1))
        pause=$((RELOCK_PAUSE << (lock_failures - 1)))
        [ "$pause" -gt 14400 ] && pause=14400
        relock_after=$((now + pause))
        locked_at=0
        log "WiFi har været nede i $down sekunder på det låste adgangspunkt; låser op" \
            "og prøver alle adgangspunkter (låser tidligst igen om $((pause / 60)) minutter)"
        set_lock "$conn" "" || true
        sleep "$CHECK_INTERVAL"
        continue
    fi
    if [ "$down" -ge "$RESET_AFTER" ] && { [ "$last_reset" -eq 0 ] || [ $((now - last_reset)) -ge "$RESET_REPEAT" ]; }; then
        reset_wifi "$iface"
        last_reset=$now
    elif [ "$down" -ge "$RECONNECT_AFTER" ] && [ "$reconnected" -eq 0 ]; then
        log "Forbinder $iface til WiFi igen"
        nmcli device connect "$iface" > /dev/null 2>&1 || true
        reconnected=1
    fi
    if [ "$REBOOT_AFTER" -gt 0 ] && [ "$down" -ge "$REBOOT_AFTER" ]; then
        maybe_reboot "$now"
    fi
    sleep "$CHECK_INTERVAL"
done
