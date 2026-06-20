#!/usr/bin/env python3
"""Get IP from AT+CGCONTRDP and configure wwan0, then test ping."""
import os, time, termios, re

AT_PORT = '/dev/wwan0at0'

# Get IP via AT
fd = os.open(AT_PORT, os.O_RDWR | os.O_NOCTTY)
attrs = termios.tcgetattr(fd)
attrs[0] = termios.IGNPAR
attrs[1] = 0
attrs[2] = termios.B115200 | termios.CS8 | termios.CLOCAL | termios.CREAD
attrs[3] = 0
attrs[4] = termios.B115200
attrs[5] = termios.B115200
attrs[6][termios.VMIN] = 0
attrs[6][termios.VTIME] = 20
termios.tcsetattr(fd, termios.TCSAFLUSH, attrs)

def at(cmd):
    termios.tcflush(fd, termios.TCIOFLUSH)
    os.write(fd, (cmd + "\r\n").encode())
    buf = b""
    for _ in range(60):
        try:
            d = os.read(fd, 4096)
            if d:
                buf += d
            if b"OK" in buf or b"ERROR" in buf:
                break
        except:
            pass
        time.sleep(0.1)
    return buf.decode(errors="replace").strip()

r = at("AT+CGCONTRDP")
print("CGCONTRDP:", r)
os.close(fd)

# Parse: +CGCONTRDP: cid,bearer,"apn","ip.mask","gw",...
m = re.search(r'CGCONTRDP: \d+,\d+,"[^"]*","([^"]+)","([^"]+)"', r)
if not m:
    print("ERROR: could not parse IP from CGCONTRDP")
    exit(1)

addr_mask = m.group(1)  # e.g. "100.75.239.16.255.0.0.0"
gw = m.group(2)
parts = addr_mask.split(".")
ip = ".".join(parts[:4])
mask = ".".join(parts[4:])
prefix = sum(bin(int(x)).count("1") for x in mask.split("."))
print(f"IP: {ip}/{prefix}  GW: {gw}")

# Configure wwan0 (running as root via gdb shell)
os.system("ip addr flush dev wwan0 2>/dev/null")
os.system(f"ip addr add {ip}/{prefix} dev wwan0")
os.system("ip link set wwan0 up")
os.system(f"ip route add default via {gw} dev wwan0 metric 700 2>/dev/null")

print("wwan0 configured. Testing ping...")
rc = os.system("ping -c 5 -W 2 -I wwan0 8.8.8.8")
print(f"ping result: {'SUCCESS' if rc == 0 else 'FAILED'}")
