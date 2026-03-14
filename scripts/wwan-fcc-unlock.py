#!/usr/bin/env python3
"""FCC unlock for Intel XMM7560 / Fibocom L860R+
   Key: DW5823EFCCLOCK
   Sequence: AT+GTFCCLOCKGEN -> AT+GTFCCLOCKVER=<sha256> -> AT+GTFCCLOCKMODEUNLOCK -> AT+CFUN=1
"""
import os, sys, time, hashlib, struct, hmac, termios, fcntl

AT_PORT = '/dev/wwan0at0'
KEY = b'DW5823EFCCLOCK'


def setup_port(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    attrs = termios.tcgetattr(fd)
    B = termios.B115200
    attrs[0] = termios.IGNPAR          # iflag: ignore parity errors
    attrs[1] = 0                        # oflag: raw output
    attrs[2] = B | termios.CS8 | termios.CLOCAL | termios.CREAD  # cflag
    attrs[3] = 0                        # lflag: raw input
    attrs[4] = B                        # ispeed
    attrs[5] = B                        # ospeed
    attrs[6][termios.VMIN]  = 0
    attrs[6][termios.VTIME] = 20        # 2-second read timeout
    termios.tcsetattr(fd, termios.TCSAFLUSH, attrs)
    return fd


def send_recv(fd, cmd, timeout=6):
    termios.tcflush(fd, termios.TCIOFLUSH)
    os.write(fd, (cmd + '\r\n').encode())
    buf = b''
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = os.read(fd, 4096)
            if chunk:
                buf += chunk
                text = buf.decode(errors='replace')
                if '\nOK' in text or 'ERROR' in text:
                    break
        except BlockingIOError:
            time.sleep(0.05)
        except OSError:
            time.sleep(0.05)
    return buf.decode(errors='replace').strip()


def parse_challenge(response):
    """Extract 32-bit challenge from AT+GTFCCLOCKGEN response.
    Firmware returns a bare hex value like:  0x5754cabc
    Some firmwares prefix it:                +GTFCCLOCKGEN: 0x5754cabc
    """
    for line in response.splitlines():
        line = line.strip()
        # strip prefix if present
        for prefix in ('+GTFCCLOCKGEN:', 'GTFCCLOCKGEN:'):
            if line.upper().startswith(prefix.upper()):
                line = line[len(prefix):].strip()
                break
        if line.lower().startswith('0x'):
            try:
                return int(line, 16)
            except ValueError:
                pass
    return None


def compute_candidates(challenge_int):
    """Return (label, decimal_value) pairs to try for AT+GTFCCLOCKVER."""
    cle = struct.pack('<I', challenge_int)
    cbe = struct.pack('>I', challenge_int)
    results = []
    for label, data in [
        ('SHA256(key||chall_le)', KEY + cle),
        ('SHA256(key||chall_be)', KEY + cbe),
        ('SHA256(chall_le||key)', cle + KEY),
        ('SHA256(chall_be||key)', cbe + KEY),
    ]:
        d = hashlib.sha256(data).digest()
        results.append((label + '_rBE', struct.unpack('>I', d[:4])[0]))
        results.append((label + '_rLE', struct.unpack('<I', d[:4])[0]))
    for label, msg in [('HMAC_SHA256(key,chall_le)', cle), ('HMAC_SHA256(key,chall_be)', cbe)]:
        d = hmac.new(KEY, msg, hashlib.sha256).digest()
        results.append((label + '_rBE', struct.unpack('>I', d[:4])[0]))
        results.append((label + '_rLE', struct.unpack('<I', d[:4])[0]))
    return results


# ── main ──────────────────────────────────────────────────────────────────────

print(f"Opening {AT_PORT} ...")
fd = setup_port(AT_PORT)
# switch to blocking I/O
flags = fcntl.fcntl(fd, fcntl.F_GETFL)
fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

print("AT sanity check ...")
r = send_recv(fd, 'AT')
print(f"  >> {repr(r)}")
if 'OK' not in r:
    print("WARNING: modem did not respond with OK — continuing anyway")

print("\nGetting FCC challenge (AT+GTFCCLOCKGEN) ...")
r = send_recv(fd, 'AT+GTFCCLOCKGEN')
print(f"  >> {repr(r)}")

challenge = parse_challenge(r)
if challenge is None:
    print("ERROR: could not parse challenge from response")
    os.close(fd)
    sys.exit(1)

print(f"  Challenge: 0x{challenge:08x} ({challenge})")

candidates = compute_candidates(challenge)
print("\nAll candidates:")
for label, val in candidates:
    print(f"  {label}: {val}  (0x{val:08x})")

print("\nTrying each candidate ...")
success_label = None

for label, val in candidates:
    # Get a fresh challenge for each attempt
    print(f"\n  [challenge refresh] AT+GTFCCLOCKGEN ...")
    r0 = send_recv(fd, 'AT+GTFCCLOCKGEN')
    fresh = parse_challenge(r0)
    if fresh is not None and fresh != challenge:
        print(f"  Challenge changed to 0x{fresh:08x} — recomputing")
        challenge = fresh
        # Recompute just the current candidate with the new challenge
        cle = struct.pack('<I', challenge)
        cbe = struct.pack('>I', challenge)
        if 'chall_le' in label:
            c = cle
        else:
            c = cbe
        if label.startswith('SHA256(key||'):
            d = hashlib.sha256(KEY + c).digest()
        elif label.startswith('SHA256(chall'):
            d = hashlib.sha256(c + KEY).digest()
        else:
            d = hmac.new(KEY, c, hashlib.sha256).digest()
        if label.endswith('_rBE'):
            val = struct.unpack('>I', d[:4])[0]
        else:
            val = struct.unpack('<I', d[:4])[0]
        print(f"  Recomputed {label}: {val}  (0x{val:08x})")

    print(f"  Trying {label} = {val} ...")
    r = send_recv(fd, f'AT+GTFCCLOCKVER={val}')
    print(f"  >> {repr(r)}")

    if 'OK' in r and 'ERROR' not in r:
        print(f"\n  *** UNLOCK RESPONSE ACCEPTED ({label}) ***")
        success_label = label
        break
    else:
        print(f"  (no match — trying next)")

if success_label is None:
    print("\nAll candidates rejected.  Unlock failed.")
    os.close(fd)
    sys.exit(1)

print("\nSending AT+GTFCCLOCKMODEUNLOCK ...")
r = send_recv(fd, 'AT+GTFCCLOCKMODEUNLOCK')
print(f"  >> {repr(r)}")

print("\nSending AT+CFUN=1 ...")
r = send_recv(fd, 'AT+CFUN=1')
print(f"  >> {repr(r)}")

print("\nDone.  Check modem state with: mmcli -m 0")
os.close(fd)
