#!/bin/sh
# TurboMac static cap: turbomac-cap.service ExecStart, turbomacd ExecStartPre/ExecStopPost.
# Usage: turbomac-cap.sh [apply|show|mark-clean|capture [--force]|init-config]
# 'apply' never exits non-zero: a failure here must not block boot or the daemon.
PY=/usr/bin/python3
SRC=/opt/turbomac/src
RAPL=/sys/class/powercap/intel-rapl:0
CONFIG=/etc/turbomac/config.json

cmd=${1:-apply}
if [ "$cmd" != "apply" ]; then
    PYTHONPATH=$SRC exec "$PY" -m turbomac.cap "$@"
fi

PYTHONPATH=$SRC timeout 15 "$PY" -m turbomac.cap apply
rc=$?
if [ "$rc" -eq 0 ]; then
    exit 0
fi
# 3 = written but readback mismatched: leave it to turbomacd. Anything else (4 = sysfs write
# error, 2 = no RAPL, 124 = timeout, 1/127 = python broken or missing) falls back to plain echo.
if [ "$rc" -eq 3 ]; then
    echo "turbomac-cap: limits written but readback mismatched; leaving them for turbomacd to retry"
    exit 0
fi
# Use governor.static_cap_w from the config if plain python3 + json can read it (10..45 W), else 35 W.
FALLBACK_UW=$("$PY" -c 'import json,sys; v=json.load(open(sys.argv[1]))["governor"]["static_cap_w"]; print(int(round(v*1e6)) if type(v) in (int,float) and 10<=v<=45 else "")' "$CONFIG" 2>/dev/null)
case "$FALLBACK_UW" in
    ''|*[!0-9]*) FALLBACK_UW=35000000 ;;
esac
echo "turbomac-cap: python apply failed (rc=$rc); writing ${FALLBACK_UW} uW to PL1 and PL2 directly"
echo "$FALLBACK_UW" > "$RAPL/constraint_0_power_limit_uw" || echo "turbomac-cap: PL1 write failed"
echo "$FALLBACK_UW" > "$RAPL/constraint_1_power_limit_uw" || echo "turbomac-cap: PL2 write failed"
echo 1 > "$RAPL/enabled" || echo "turbomac-cap: enable write failed"
echo "turbomac-cap: readback PL1=$(cat "$RAPL/constraint_0_power_limit_uw" 2>/dev/null)" \
     "PL2=$(cat "$RAPL/constraint_1_power_limit_uw" 2>/dev/null)" \
     "enabled=$(cat "$RAPL/enabled" 2>/dev/null)"
exit 0
