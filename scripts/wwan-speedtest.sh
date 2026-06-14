#!/bin/bash
# Measure LTE throughput (ping, download, upload) bound to wwan0.
#
# Uses curl --interface (SO_BINDTODEVICE) so traffic is forced through
# wwan0 regardless of the routing table. No sudo required.
#
# Speed test endpoints: Cloudflare (speed.cloudflare.com)

set -euo pipefail

IFACE="wwan0"
CF_HOST="speed.cloudflare.com"
DOWNLOAD_BYTES=20000000   # 20 MB
UPLOAD_BYTES=10000000     # 10 MB

die() { echo "Error: $*" >&2; exit 1; }
bps_to_mbps() { awk "BEGIN { printf \"%.2f\", $1 * 8 / 1000000 }"; }

ip link show "$IFACE" > /dev/null 2>&1 || die "$IFACE interface not found"
IFACE_IP=$(ip -4 addr show "$IFACE" | grep -oP '(?<=inet )\d+\.\d+\.\d+\.\d+' | head -1)
[ -n "$IFACE_IP" ] || die "$IFACE has no IPv4 address (is LTE connected?)"
command -v curl > /dev/null 2>&1 || die "curl not found"

echo "LTE speed test via $IFACE ($IFACE_IP)"
echo "Server: $CF_HOST"
echo ""

# ── ping latency ──────────────────────────────────────────────────────────────
printf "Ping latency:  "
PING_MS=$(ping -I "$IFACE" -c 10 -q "$CF_HOST" 2>/dev/null \
    | grep -oP 'avg.*= \K[\d.]+' | head -1)
if [ -n "$PING_MS" ]; then
    echo "${PING_MS} ms (avg of 10)"
else
    echo "n/a"
fi

# ── download ──────────────────────────────────────────────────────────────────
printf "Download:      "
DOWN_BPS=$(curl --interface "$IFACE" -s -o /dev/null \
    -w '%{speed_download}' --max-time 60 \
    "https://${CF_HOST}/__down?bytes=${DOWNLOAD_BYTES}")
echo "$(bps_to_mbps "$DOWN_BPS") Mbit/s  ($(awk "BEGIN{printf \"%.1f\", $DOWN_BPS/1000000}") MB/s)"

# ── upload ────────────────────────────────────────────────────────────────────
printf "Upload:        "
UP_BPS=$(dd if=/dev/urandom bs=1M count=$(( UPLOAD_BYTES / 1000000 )) 2>/dev/null \
    | curl --interface "$IFACE" -s -o /dev/null \
        -w '%{speed_upload}' --max-time 60 \
        -T - "https://${CF_HOST}/__up")
echo "$(bps_to_mbps "$UP_BPS") Mbit/s  ($(awk "BEGIN{printf \"%.1f\", $UP_BPS/1000000}") MB/s)"

echo ""
