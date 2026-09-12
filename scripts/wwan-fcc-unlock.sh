#!/bin/bash
# FCC unlock wrapper for Intel XMM7560.
# Strategy: wait for ModemManager to initialize the modem (AT port becomes responsive),
# then stop MM, do direct FCC unlock, restart MM.

log() { logger "wwan-fcc-unlock: $*"; echo "$*"; }

# Unbuffered stdout: without this, wwan-fcc-unlock.py's print() output is
# fully block-buffered (stdout isn't a TTY under systemd) and only reaches
# the journal when the process exits normally or the buffer fills. If the
# script hangs and gets killed by the per-attempt timeout below, none of
# its progress would otherwise show up — see the 2026-09-12 boot hang.
export PYTHONUNBUFFERED=1

# Bound each attempt so a single hang can't eat the whole service timeout
# and starve the retry — also what let systemd's TimeoutStartSec come down
# from 300s to 150s (see systemd/wwan-fcc-unlock.service).
ATTEMPT_TIMEOUT=30

log "waiting for ModemManager to detect modem"
detected=0
for i in $(seq 60); do
    if mmcli -L 2>/dev/null | grep -q 'Modem/'; then
        detected=1; break
    fi
    sleep 2
done

if [ $detected -eq 0 ]; then
    log "modem not detected by ModemManager after 120s, aborting"
    exit 1
fi

log "modem detected by MM, waiting 5s for AT port initialization"
sleep 5

log "stopping ModemManager to release AT port"
systemctl stop ModemManager.service
sleep 2

# Verify AT port is available
if [ ! -c /dev/wwan0at0 ]; then
    log "AT port /dev/wwan0at0 not found after stopping MM"
    systemctl start ModemManager.service
    exit 1
fi

log "running FCC unlock (attempt 1)"
timeout "$ATTEMPT_TIMEOUT" python3 /usr/local/bin/wwan-fcc-unlock.py
rc=$?
if [ $rc -eq 0 ]; then
    log "FCC unlock succeeded"
    log "restarting ModemManager"
    systemctl start ModemManager.service
    sleep 10
    exit 0
fi
[ $rc -eq 124 ] && log "attempt 1 timed out after ${ATTEMPT_TIMEOUT}s" || log "attempt 1 failed (exit $rc)"
log "retrying after 5s"
sleep 5

log "running FCC unlock (attempt 2)"
timeout "$ATTEMPT_TIMEOUT" python3 /usr/local/bin/wwan-fcc-unlock.py
rc=$?
if [ $rc -eq 0 ]; then
    log "FCC unlock succeeded on attempt 2"
    log "restarting ModemManager"
    systemctl start ModemManager.service
    sleep 10
    exit 0
fi
[ $rc -eq 124 ] && log "attempt 2 timed out after ${ATTEMPT_TIMEOUT}s" || log "attempt 2 failed (exit $rc)"

log "FCC unlock failed after 2 attempts, restarting ModemManager anyway"
systemctl start ModemManager.service
exit 1
