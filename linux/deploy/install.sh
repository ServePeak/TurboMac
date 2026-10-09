#!/bin/sh
# TurboMac installer for old-mac (MacBookPro14,3, Ubuntu Server). Idempotent.
# Usage: sudo sh deploy/install.sh [--force] [--firmware-limits FILE]
#   --firmware-limits FILE  pre-seed /var/lib/turbomac/firmware-limits.json (if absent) with
#                           known-true firmware values, for a machine with a manual cap active
set -eu

usage() { echo "usage: install.sh [--force] [--firmware-limits FILE]" >&2; exit 2; }
FORCE=0
FW_SEED=""
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1 ;;
        --firmware-limits) [ $# -ge 2 ] || usage; FW_SEED=$2; shift ;;
        --firmware-limits=*) FW_SEED=${1#--firmware-limits=} ;;
        *) usage ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || { echo "install.sh: must run as root" >&2; exit 1; }

EXPECTED="Intel(R) Core(TM) i7-7820HQ"
MODEL=$(sed -n 's/^model name[[:space:]]*: //p' /proc/cpuinfo | head -n 1)
case "$MODEL" in
    *"$EXPECTED"*) ;;
    *)
        if [ "$FORCE" -eq 1 ]; then
            echo "WARNING: CPU is '$MODEL', not $EXPECTED; continuing because of --force"
        else
            echo "install.sh: CPU is '$MODEL', expected $EXPECTED. Refusing (use --force)." >&2
            exit 1
        fi ;;
esac

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
PREFIX=/opt/turbomac
PY=/usr/bin/python3
OWNER=${TURBOMAC_USER:-parham}

[ -x "$PY" ] || { echo "install.sh: $PY not found" >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || { echo "install.sh: python3 >= 3.10 required" >&2; exit 1; }
if [ -n "$FW_SEED" ]; then
    "$PY" -c 'import json, sys
d = json.load(open(sys.argv[1]))
num = lambda v: type(v) in (int, float) and v > 0
sys.exit(0 if isinstance(d, dict) and all(num(d.get(k)) for k in ("pl1_w", "pl2_w", "pl1_uw", "pl2_uw")) else 1)' \
        "$FW_SEED" 2>/dev/null \
        || { echo "install.sh: $FW_SEED is not JSON with numeric pl1_w/pl2_w/pl1_uw/pl2_uw; refusing" >&2; exit 1; }
fi

echo "== code -> $PREFIX"
install -d -m 0755 "$PREFIX" "$PREFIX/bin" "$PREFIX/src"
if [ "$(realpath "$ROOT/turbomac")" != "$(realpath -m "$PREFIX/src/turbomac")" ]; then
    rm -rf "$PREFIX/src/turbomac.new"
    cp -a "$ROOT/turbomac" "$PREFIX/src/turbomac.new"
    rm -rf "$PREFIX/src/turbomac"
    mv "$PREFIX/src/turbomac.new" "$PREFIX/src/turbomac"
    [ -f "$ROOT/README.md" ] && install -m 0644 "$ROOT/README.md" "$PREFIX/src/README.md"
fi
find "$PREFIX/src/turbomac" -name __pycache__ -type d -prune -exec rm -rf {} +
install -m 0755 "$HERE/turbomac-cap.sh" "$PREFIX/bin/turbomac-cap.sh"
for pair in turbomacd:turbomac.daemon turbomacctl:turbomac.ctl; do
    name=${pair%%:*}
    mod=${pair#*:}
    printf '#!/bin/sh\nPYTHONPATH=%s/src exec %s -m %s "$@"\n' "$PREFIX" "$PY" "$mod" > "$PREFIX/bin/$name"
    chmod 0755 "$PREFIX/bin/$name"
    ln -sf "$PREFIX/bin/$name" "/usr/local/bin/$name"
done

echo "== group turbomac"
install -d -m 0755 /etc/sysusers.d
install -m 0644 "$HERE/sysusers.conf" /etc/sysusers.d/turbomac.conf
systemd-sysusers /etc/sysusers.d/turbomac.conf 2>/dev/null || true
getent group turbomac >/dev/null || groupadd --system turbomac
if id "$OWNER" >/dev/null 2>&1; then
    usermod -aG turbomac "$OWNER"
    echo "user $OWNER is in group turbomac (log in again to pick it up)"
fi

echo "== kernel modules"
install -m 0644 "$HERE/modules-load.conf" /etc/modules-load.d/turbomac.conf
modprobe msr || echo "WARNING: modprobe msr failed"
modprobe intel_rapl_msr || echo "WARNING: modprobe intel_rapl_msr failed"

echo "== config and state"
install -d -m 0755 /etc/turbomac /var/lib/turbomac
PYTHONPATH="$PREFIX/src" "$PY" -m turbomac.cap init-config
FW_FILE=/var/lib/turbomac/firmware-limits.json
if [ -n "$FW_SEED" ]; then
    if [ -e "$FW_FILE" ]; then
        echo "$FW_FILE already exists; --firmware-limits ignored (delete it first to re-seed)"
    else
        install -m 0644 "$FW_SEED" "$FW_FILE"
        echo "seeded $FW_FILE from $FW_SEED"
    fi
fi
# Firmware limits must be captured before TurboMac writes any limit (no-op if already captured or seeded).
PYTHONPATH="$PREFIX/src" "$PY" -m turbomac.cap capture

echo "== systemd units"
WAS_ACTIVE=$(systemctl is-active turbomacd.service 2>/dev/null || true)
install -m 0644 "$HERE/turbomac-cap.service" "$HERE/turbomacd.service" /etc/systemd/system/
systemctl daemon-reload
systemctl mask --now thermald.service 2>/dev/null || true
systemctl enable --now turbomac-cap.service turbomacd.service
if [ "$WAS_ACTIVE" = "active" ]; then
    systemctl restart turbomac-cap.service turbomacd.service
fi

echo "== smoke test"
sleep 2
/usr/local/bin/turbomacctl status

echo "== turbomactop (TUI venv, optional)"
# The daemon never uses the venv; a TUI problem never fails the install.
VENV="$PREFIX/venv"
if "$PY" -c 'import ensurepip' 2>/dev/null; then
    if { [ -x "$VENV/bin/pip" ] || "$PY" -m venv "$VENV"; } \
        && PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1 \
           "$VENV/bin/pip" install -q -r "$ROOT/requirements-tui.txt"; then
        printf '#!/bin/sh\nPYTHONPATH=%s/src exec %s/bin/python -m turbomac.tui "$@"\n' "$PREFIX" "$VENV" \
            > "$PREFIX/bin/turbomactop"
        chmod 0755 "$PREFIX/bin/turbomactop"
        ln -sf "$PREFIX/bin/turbomactop" /usr/local/bin/turbomactop
        echo "turbomactop installed: run 'turbomactop' as $OWNER (group turbomac)"
    else
        echo "WARNING: TUI venv setup failed (network?); the daemon is unaffected. Re-run install.sh to retry."
    fi
else
    echo "TUI skipped: run 'apt-get install -y python3-venv' and re-run install.sh"
fi
