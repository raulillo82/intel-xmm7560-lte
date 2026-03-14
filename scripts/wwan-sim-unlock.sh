#!/bin/bash
# Unlock SIM PIN via ModemManager before display manager starts.
# Reads PIN from NM connection file (root-only readable).

log() { logger "wwan-sim-unlock: $*"; echo "$*"; }

PIN_FILE="/etc/NetworkManager/system-connections/Orange.nmconnection"

PIN=$(grep -E '^pin=' "$PIN_FILE" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
if [ -z "$PIN" ]; then
    log "no PIN found in $PIN_FILE, skipping"
    exit 0
fi

log "waiting for modem in ModemManager"
MODEM_IDX=""
for i in $(seq 30); do
    MODEM_IDX=$(mmcli -L 2>/dev/null | grep -oP '(?<=/Modem/)\d+' | head -1)
    [ -n "$MODEM_IDX" ] && break
    sleep 2
done

if [ -z "$MODEM_IDX" ]; then
    log "no modem found after 60s, skipping"
    exit 0
fi

log "modem index $MODEM_IDX, waiting for SIM-locked state"
SIM_IDX=""
locked=0
for i in $(seq 20); do
    STATE=$(mmcli -m "$MODEM_IDX" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | grep -oP '(?<=state: )\S+' | head -1)
    if [ "$STATE" = "locked" ]; then
        SIM_IDX=$(mmcli -m "$MODEM_IDX" 2>/dev/null | grep -oP '(?<=/SIM/)\d+' | head -1)
        locked=1; break
    fi
    sleep 2
done

if [ $locked -eq 0 ]; then
    log "modem never entered locked state (already unlocked?), skipping"
    exit 0
fi

if [ -z "$SIM_IDX" ]; then
    log "could not find SIM index, skipping"
    exit 1
fi

log "modem state locked, sending PIN to SIM $SIM_IDX"
if mmcli -i "$SIM_IDX" --pin="$PIN" 2>&1; then
    log "PIN accepted — SIM unlocked"
else
    log "PIN rejected or error"
fi
