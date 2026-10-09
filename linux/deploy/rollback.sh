#!/bin/sh
# TurboMac rollback: stop and disable the units, unmask thermald, restore the
# captured firmware RAPL limits and GPU defaults. Logs, incidents and config are kept.
# Usage: sudo sh deploy/rollback.sh [--purge]
#   --purge  also remove /etc/modules-load.d/turbomac.conf, /etc/sysusers.d/turbomac.conf,
#            the /usr/local/bin symlinks, the turbomac group and the TUI venv /opt/turbomac/venv
set -u
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        *) echo "usage: rollback.sh [--purge]" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo "rollback.sh: must run as root" >&2; exit 1; }

RAPL=/sys/class/powercap/intel-rapl:0
FW=/var/lib/turbomac/firmware-limits.json
SW=/sys/kernel/debug/vgaswitcheroo/switch

echo "== stopping units"
# Stopping turbomacd re-applies the static cap (ExecStopPost); firmware limits are restored below.
systemctl disable --now turbomacd.service turbomac-cap.service || true

echo "== thermald"
systemctl unmask thermald.service 2>/dev/null || true
systemctl start thermald.service 2>/dev/null || true

echo "== RAPL limits"
PL1=100000000
PL2=125000000
# Only a valid, non-suspect capture (numeric, PL1 >= 45 W) is trusted; otherwise the verified defaults.
VALS=$(/usr/bin/python3 -c 'import json,sys
d = json.load(open(sys.argv[1]))
num = lambda v: type(v) in (int, float) and v > 0
ok = isinstance(d, dict) and not d.get("suspect") and num(d.get("pl1_uw")) and num(d.get("pl2_uw")) and d["pl1_uw"] >= 45000000
print("%d %d" % (d["pl1_uw"], d["pl2_uw"]) if ok else "")' "$FW" 2>/dev/null)
case "$VALS" in
    *[0-9]" "[0-9]*) PL1=${VALS% *}; PL2=${VALS#* } ;;
    *) echo "WARNING: $FW missing, suspect or invalid; using the verified firmware defaults 100 W / 125 W" ;;
esac
echo "$PL1" > "$RAPL/constraint_0_power_limit_uw"
echo "$PL2" > "$RAPL/constraint_1_power_limit_uw"
echo 1 > "$RAPL/enabled"
echo "PL1=$(cat "$RAPL/constraint_0_power_limit_uw") PL2=$(cat "$RAPL/constraint_1_power_limit_uw")" \
     "enabled=$(cat "$RAPL/enabled")"

echo "== GPU"
if grep -q '^[0-9]*:DIS: :Off' "$SW" 2>/dev/null; then
    echo ON > "$SW" && echo DIS > "$SW" && echo "dGPU powered back on"
    for vt in /sys/class/vtconsole/vtcon*; do
        grep -q "frame buffer" "$vt/name" 2>/dev/null && echo 1 > "$vt/bind"
    done
fi
for dev in /sys/class/drm/card*/device; do
    [ "$(cat "$dev/vendor" 2>/dev/null)" = "0x1002" ] || continue
    for hw in "$dev"/hwmon/hwmon*; do
        [ -f "$hw/power1_cap_max" ] && cat "$hw/power1_cap_max" > "$hw/power1_cap" \
            && echo "power1_cap=$(cat "$hw/power1_cap")"
    done
    echo auto > "$dev/power_dpm_force_performance_level" && echo "dpm level auto"
done

if [ "$PURGE" -eq 1 ]; then
    echo "== purge"
    rm -f /etc/modules-load.d/turbomac.conf /etc/sysusers.d/turbomac.conf \
          /usr/local/bin/turbomacctl /usr/local/bin/turbomacd
    if getent group turbomac >/dev/null; then
        groupdel turbomac && echo "group turbomac removed"
    fi
    if [ -d /opt/turbomac/venv ]; then
        rm -rf /opt/turbomac/venv && echo "TUI venv /opt/turbomac/venv removed"
    fi
fi

echo "rollback done. Logs and incidents kept in /var/lib/turbomac; config in /etc/turbomac."
echo "Unit files remain in /etc/systemd/system (disabled); code in /opt/turbomac."
if [ "$PURGE" -eq 0 ]; then
    echo "Also kept: modules-load and sysusers entries, /usr/local/bin symlinks, group turbomac (--purge removes them)."
fi
echo "A reboot restores every hardware default."
