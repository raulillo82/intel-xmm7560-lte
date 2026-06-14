# Intel XMM7560 / Fibocom L860R+ LTE on Linux

Full LTE automation for the Intel XMM7560 modem (Fibocom L860R+, Dell DW5823e) on Linux: FCC unlock at boot, automatic SIM PIN entry before the desktop starts, and IP configuration via AT commands.

Developed and tested on:

| Component | Value |
|-----------|-------|
| Device | Lenovo ThinkPad P14s Gen 4 |
| Modem | Intel XMM7560 / Fibocom L860R+ |
| PCI ID | `8086:7560`, subsystem `1cf8:8655` |
| Driver | `iosm` (Intel Out-of-band Software Modem) |
| Kernel | 6.19.x |
| ModemManager | 1.24.2 |
| OS | openSUSE Tumbleweed |
| SIM / Carrier | Orange Spain |

---

## Background

The Intel XMM7560 carries an **FCC hardware lock** that must be lifted on every power-on before the modem can register on any network. The lock is lifted via a challenge-response protocol over the AT interface, documented below.

Additionally, this modem's firmware does **not** return IP configuration through the MBIM `IP_CONFIGURATION` message. The IP address, netmask, and gateway must be retrieved via the AT command `AT+CGCONTRDP` and applied manually to the `wwan0` interface.

---

## FCC Unlock — how it works

The modem uses Intel's `GTFCCLOCKGEN`/`GTFCCLOCKVER` AT command pair:

1. Send `AT+GTFCCLOCKGEN` — modem returns a 32-bit hex challenge, e.g. `0x5754cabc`
2. Compute the response:
   ```
   response = SHA256(b"DW5823EFCCLOCK" + challenge_as_4_bytes_little_endian)
   value    = first 4 bytes of response, interpreted as big-endian unsigned int
   ```
3. Send `AT+GTFCCLOCKVER=<decimal_value>`
4. If accepted (`OK`): send `AT+GTFCCLOCKMODEUNLOCK`, then `AT+CFUN=1`

`wwan-fcc-unlock.py` tries multiple byte-ordering candidates to be robust across firmware variants. In practice the algorithm above (`SHA256(key_LE + challenge_LE)` → first 4 bytes BE) is the correct one for this hardware.

---

## Boot sequence

```
ModemManager starts
  └─► wwan-fcc-unlock.service   (After=ModemManager)
        Waits for MM to detect the modem (ensures AT port is initialised by the driver)
        Stops ModemManager (releases /dev/wwan0at0)
        Runs wwan-fcc-unlock.py directly on the AT port
        Restarts ModemManager

  └─► wwan-sim-unlock.service   (After=wwan-fcc-unlock, Before=display-manager)
        Waits for modem state: locked (SIM PIN required)
        Sends PIN via:  mmcli -i <SIM_IDX> --pin=<PIN>
        SIM is unlocked before the desktop starts → no PIN dialog in KDE/GNOME
```

When the user connects from the desktop network applet, the NM dispatcher script runs `wwan-setup-ip.py` to configure the `wwan0` interface.

> **Why stop ModemManager for the unlock?**
> The AT port exists as soon as the `iosm` driver loads, but the modem only responds
> to AT commands after ModemManager has initialised it. Running the unlock *before* MM
> consistently times out. The working approach is: let MM initialise the modem, stop MM
> to release the port, unlock, restart MM.

---

## Prerequisites

- `ModemManager` + `mmcli`
- `NetworkManager` + `nmcli`
- `python3` (standard library only)
- A NetworkManager GSM connection profile for your carrier

---

## Installation

```bash
git clone https://github.com/raulillo82/intel-xmm7560-lte
cd intel-xmm7560-lte
sudo bash install.sh
```

Then store your SIM PIN in the NetworkManager connection so it is available to the SIM unlock service at boot:

```bash
sudo nmcli connection modify <connection-name> gsm.pin <PIN>
```

If your connection is not named `Orange`, edit `networkmanager/dispatcher.d/99-wwan-ip` to match before installing.

---

## Carrier configuration

| Field | Value |
|-------|-------|
| APN | `<your-apn>` |
| Username | `<your-username>` |
| Password | `<your-password>` |
| DNS | `8.8.8.8`, `8.8.4.4` (set `ignore-auto-dns=yes`) |
| IPv6 | ignore |
| Autoconnect | no (connect manually from the desktop applet) |

Your carrier's APN settings are usually published on their website or sent via SMS on first SIM insertion.

---

## Testing LTE speed

```bash
scripts/wwan-speedtest.sh
```

Binds `speedtest-cli` to the `wwan0` interface so the test runs exclusively over LTE (not your default route). Pass any extra flags directly to `speedtest-cli`, e.g. `--simple` for a shorter one-liner output.

Requires `speedtest-cli` (`zypper install speedtest-cli` or `pip install speedtest-cli`).

---

## Reading SMS messages

```bash
# via ModemManager (default — ModemManager must be running)
python3 scripts/wwan-sms-read.py

# via AT port directly (use when ModemManager is stopped)
sudo python3 scripts/wwan-sms-read.py --at
```

Lists all SMS stored in the modem/SIM and prints sender, timestamp and body for each one. The AT fallback mode (`--at`) bypasses ModemManager and talks to `/dev/wwan0at0` directly — useful during manual recovery when MM is stopped.

---

## Files

| File | Installed path | Description |
|------|---------------|-------------|
| `scripts/wwan-fcc-unlock.py` | `/usr/local/bin/` | FCC challenge-response unlock via direct AT port |
| `scripts/wwan-fcc-unlock.sh` | `/usr/local/bin/` | Boot wrapper: stop MM → unlock → restart MM |
| `scripts/wwan-sim-unlock.sh` | `/usr/local/bin/` | Sends SIM PIN via mmcli before display manager starts |
| `scripts/wwan-setup-ip.py` | `/usr/local/bin/` | Reads IP from `AT+CGCONTRDP`, configures `wwan0` |
| `systemd/wwan-fcc-unlock.service` | `/etc/systemd/system/` | Systemd unit for FCC unlock |
| `systemd/wwan-sim-unlock.service` | `/etc/systemd/system/` | Systemd unit for SIM PIN unlock |
| `networkmanager/dispatcher.d/99-wwan-ip` | `/etc/NetworkManager/dispatcher.d/` | Configures `wwan0` IP when connection activates |
| `scripts/wwan-speedtest.sh` | run directly | Tests LTE down/up speed via `speedtest-cli`, bound to `wwan0` |
| `scripts/wwan-sms-read.py` | run directly | Lists and reads SMS messages via mmcli or AT port |

---

## Manual recovery

If the modem gets stuck (MBIM timeouts, no response to AT commands):

```bash
# Stop services
sudo systemctl stop ModemManager NetworkManager

# PCI power toggle — resets modem firmware
echo auto | sudo tee /sys/bus/pci/devices/0000:01:00.0/power/control
sleep 3
echo on  | sudo tee /sys/bus/pci/devices/0000:01:00.0/power/control
sleep 5

# FCC unlock manually
sudo chmod 666 /dev/wwan0at0 /dev/wwan0mbim0
sudo python3 /usr/local/bin/wwan-fcc-unlock.py

# Restart services
sudo systemctl start ModemManager NetworkManager
sleep 10

# Connect
sudo nmcli connection up <your-carrier>
# or use the desktop network applet — wwan-setup-ip.py runs automatically
```

---

## Checking connectivity

```bash
ping -c 3 -I wwan0 8.8.8.8
curl --interface wwan0 https://ipinfo.io/ip
```

---

*Developed with the assistance of [Claude](https://claude.ai) (Anthropic).*
