#!/bin/sh
# TurboMac rollback: stop and disable the units, unmask thermald, restore the
# captured firmware RAPL limits, GPU defaults and fan minimums. Logs, incidents and config are kept.
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
FANDEF=/var/lib/turbomac/fan-defaults.json

echo "== stopping units"
# Stopping turbomacd re-applies the static cap (ExecStopPost); firmware limits are restored below.
systemctl disable --now turbomacd.service turbomac-cap.service || true

echo "== thermald"
systemctl unmask thermald.service 2>/dev/null || true
systemctl start thermald.service 2>/dev/null || true

echo "== RAPL limits"
# best-known firmware limits for the i7-8750H (MacBookPro15,1); a real capture from
# /var/lib/turbomac/firmware-limits.json (taken before TurboMac wrote anything) always wins.
PL1=45000000
PL2=56000000
# Only a valid, non-suspect capture (numeric, PL1 >= 45 W) is trusted; otherwise the verified defaults.
VALS=$(/usr/bin/python3 -c 'import json,sys
d = json.load(open(sys.argv[1]))
num = lambda v: type(v) in (int, float) and v > 0
ok = isinstance(d, dict) and not d.get("suspect") and num(d.get("pl1_uw")) and num(d.get("pl2_uw")) and d["pl1_uw"] >= 45000000
print("%d %d" % (d["pl1_uw"], d["pl2_uw"]) if ok else "")' "$FW" 2>/dev/null)
case "$VALS" in
    *[0-9]" "[0-9]*) PL1=${VALS% *}; PL2=${VALS#* } ;;
    *) echo "WARNING: $FW missing, suspect or invalid; using the best-known firmware defaults 45 W / 56 W" ;;
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

echo "== fans"
# A clean daemon stop already restores the fan minimums; this also covers a crash that left a floor.
# fan-defaults.json holds the fan*_min values captured before the daemon ever wrote a floor. Fallback when
# it is missing, or for an entry that is missing or implausible (more than 500 rpm above the hardware
# minimum): the verified hardware minimums of this machine (same as the MacBookPro14,3
# reference: fan1 2160 rpm, fan2 2000 rpm).
# Only fan*_min is written; fan*_manual and fan*_output are never touched.
SMCDIR=$(ls -d /sys/devices/platform/applesmc.* 2>/dev/null | head -n 1)
if [ -n "$SMCDIR" ]; then
    FANVALS=$(/usr/bin/python3 -c 'import json,sys
known = {1: 2160, 2: 2000}
try:
    saved = json.load(open(sys.argv[1]))["fans"]
except Exception:
    saved = {}
out = dict(known)
for k, v in (saved.items() if isinstance(saved, dict) else ()):
    m = v.get("min") if isinstance(v, dict) else None
    if k.isdigit() and type(m) is int and 0 < m <= known.get(int(k), m) + 500:
        out[int(k)] = m
print(" ".join("%d=%d" % kv for kv in sorted(out.items())))' "$FANDEF" 2>/dev/null)
    [ -n "$FANVALS" ] || FANVALS="1=2160 2=2000"
    [ -f "$FANDEF" ] || echo "NOTE: $FANDEF missing; using the verified hardware minimums 2160 / 2000 rpm"
    for kv in $FANVALS; do
        n=${kv%=*}
        rpm=${kv#*=}
        [ -f "$SMCDIR/fan${n}_min" ] || continue
        echo "$rpm" > "$SMCDIR/fan${n}_min" && echo "fan${n}_min=$(cat "$SMCDIR/fan${n}_min")"
    done
else
    echo "applesmc not found; fans left alone"
fi

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
