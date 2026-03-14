#!/bin/bash
# FCC unlock wrapper for Intel XMM7560.
# Strategy: wait for ModemManager to initialize the modem (AT port becomes responsive),
# then stop MM, do direct FCC unlock, restart MM.

log() { logger "wwan-fcc-unlock: $*"; echo "$*"; }

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
if python3 /usr/local/bin/wwan-fcc-unlock.py; then
    log "FCC unlock succeeded"
    log "restarting ModemManager"
    systemctl start ModemManager.service
    sleep 10
    exit 0
fi

log "attempt 1 failed, retrying after 5s"
sleep 5

log "running FCC unlock (attempt 2)"
if python3 /usr/local/bin/wwan-fcc-unlock.py; then
    log "FCC unlock succeeded on attempt 2"
    log "restarting ModemManager"
    systemctl start ModemManager.service
    sleep 10
    exit 0
fi

log "FCC unlock failed after 2 attempts, restarting ModemManager anyway"
systemctl start ModemManager.service
exit 1
