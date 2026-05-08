#!/usr/bin/env python3
"""FCC unlock for Intel XMM7560 via ModemManager mmcli (avoids direct AT port race).
   Key: DW5823EFCCLOCK
   Sequence: AT+GTFCCLOCKGEN -> AT+GTFCCLOCKVER=<sha256> -> AT+GTFCCLOCKMODEUNLOCK -> AT+CFUN=1
"""
import subprocess, sys, time, hashlib, struct, hmac, re

KEY = b'DW5823EFCCLOCK'


def get_modem_idx():
    r = subprocess.run(['mmcli', '-L'], capture_output=True, text=True)
    m = re.search(r'/org/freedesktop/ModemManager1/Modem/(\d+)', r.stdout)
    return int(m.group(1)) if m else None


def mmcli_at(modem_idx, at_cmd, timeout=10):
    """Send AT command via mmcli --command. Returns raw stdout."""
    r = subprocess.run(
        ['mmcli', '-m', str(modem_idx), f'--command={at_cmd}', f'--timeout={timeout}'],
        capture_output=True, text=True
    )
    return r.stdout.strip()


def parse_mmcli_response(output):
    """Extract inner response text from mmcli --command output.
    Typical output:  response: '+GTFCCLOCKGEN: 0x5754cabc'
    """
    m = re.search(r"response:\s+'(.*?)'", output, re.DOTALL)
    if m:
        return m.group(1)
    return output


def parse_challenge(text):
    for line in text.splitlines():
        line = line.strip()
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


def recompute_one(label, val, challenge):
    cle = struct.pack('<I', challenge)
    cbe = struct.pack('>I', challenge)
    c = cle if 'chall_le' in label else cbe
    if label.startswith('SHA256(key||'):
        d = hashlib.sha256(KEY + c).digest()
    elif label.startswith('SHA256(chall'):
        d = hashlib.sha256(c + KEY).digest()
    else:
        d = hmac.new(KEY, c, hashlib.sha256).digest()
    return struct.unpack('>I', d[:4])[0] if label.endswith('_rBE') else struct.unpack('<I', d[:4])[0]


# ── Wait for modem ────────────────────────────────────────────────────────────

print("Waiting for ModemManager to detect modem...")
modem_idx = None
for i in range(60):
    modem_idx = get_modem_idx()
    if modem_idx is not None:
        break
    if i % 5 == 0:
        print(f"  ... still waiting ({i*2}s)")
    time.sleep(2)

if modem_idx is None:
    print("ERROR: No modem found in ModemManager after 120s")
    sys.exit(1)

print(f"Modem index: {modem_idx}")

# ── Check FCC lock state ──────────────────────────────────────────────────────

status = subprocess.run(['mmcli', '-m', str(modem_idx)], capture_output=True, text=True).stdout
print("Modem status snippet:")
for line in status.splitlines():
    if any(kw in line.lower() for kw in ['fcc', 'state', 'lock', 'power']):
        print(f"  {line.strip()}")

# ── Get FCC challenge ─────────────────────────────────────────────────────────

print("\nGetting FCC challenge (AT+GTFCCLOCKGEN)...")
raw = mmcli_at(modem_idx, 'AT+GTFCCLOCKGEN')
print(f"  mmcli raw: {repr(raw)}")
resp_text = parse_mmcli_response(raw)
print(f"  response:  {repr(resp_text)}")

challenge = parse_challenge(resp_text)
if challenge is None:
    print("Could not parse challenge — modem may already be FCC-unlocked or command not supported")
    # Check if it returned OK with no challenge (already unlocked)
    if 'OK' in resp_text and '+GTFCCLOCKGEN' not in resp_text:
        print("Modem responded OK without challenge — likely already unlocked")
        sys.exit(0)
    sys.exit(1)

print(f"  Challenge: 0x{challenge:08x} ({challenge})")

candidates = compute_candidates(challenge)
print("\nAll candidates:")
for label, val in candidates:
    print(f"  {label}: {val}  (0x{val:08x})")

# ── Try each candidate ────────────────────────────────────────────────────────

print("\nTrying each candidate...")
success_label = None

for label, val in candidates:
    # Refresh challenge
    raw0 = mmcli_at(modem_idx, 'AT+GTFCCLOCKGEN')
    resp0 = parse_mmcli_response(raw0)
    fresh = parse_challenge(resp0)
    if fresh is not None and fresh != challenge:
        print(f"  Challenge changed to 0x{fresh:08x} — recomputing")
        challenge = fresh
        val = recompute_one(label, val, challenge)
        print(f"  Recomputed {label}: {val}  (0x{val:08x})")

    print(f"  Trying {label} = {val} ...")
    raw = mmcli_at(modem_idx, f'AT+GTFCCLOCKVER={val}')
    resp = parse_mmcli_response(raw)
    print(f"  >> {repr(resp)}")

    if 'OK' in resp and 'ERROR' not in resp:
        print(f"\n  *** UNLOCK RESPONSE ACCEPTED ({label}) ***")
        success_label = label
        break
    else:
        print("  (no match — trying next)")

if success_label is None:
    print("\nAll candidates rejected. Unlock failed.")
    sys.exit(1)

# ── Finalize unlock ───────────────────────────────────────────────────────────

print("\nSending AT+GTFCCLOCKMODEUNLOCK...")
raw = mmcli_at(modem_idx, 'AT+GTFCCLOCKMODEUNLOCK')
print(f"  >> {repr(parse_mmcli_response(raw))}")

print("\nSending AT+CFUN=1 (modem will reset)...")
raw = mmcli_at(modem_idx, 'AT+CFUN=1', timeout=30)
print(f"  >> {repr(parse_mmcli_response(raw))}")

print("\nDone. Modem resetting — ModemManager will re-detect it.")
