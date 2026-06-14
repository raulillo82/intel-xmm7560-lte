#!/usr/bin/env python3
"""
Read and manage SMS messages from the LTE modem.

Default mode: uses mmcli (ModemManager must be running).
AT mode (--at): talks directly to the AT port. The script stops
ModemManager, adjusts port permissions, and restores everything on
exit — using only specific sudo commands (chmod on the AT device,
systemctl for MM), not sudo on itself.
"""

import argparse
import re
import select
import subprocess
import sys
import termios
import time


ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
AT_PORT_DEFAULT = '/dev/wwan0at0'


def strip_ansi(s):
    return ANSI_RE.sub('', s)


def die(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def sudo_run(*cmd, check=True):
    result = subprocess.run(['sudo', '-n', *cmd], capture_output=True, text=True)
    if check and result.returncode != 0:
        die(f"'{' '.join(cmd)}' failed: {result.stderr.strip()}")
    return result


# ── mmcli mode ────────────────────────────────────────────────────────────────

def mmcli_find_modem():
    out = subprocess.check_output(['mmcli', '-L'], stderr=subprocess.DEVNULL, text=True)
    m = re.search(r'/org/freedesktop/ModemManager1/Modem/(\d+)', out)
    if not m:
        die("no modem found via mmcli. Is ModemManager running?")
    return m.group(1)


def mmcli_list_paths(modem_idx):
    out = subprocess.check_output(
        ['mmcli', '-m', modem_idx, '--messaging-list-sms'],
        stderr=subprocess.DEVNULL, text=True
    )
    return re.findall(r'/org/freedesktop/ModemManager1/SMS/\d+', out)


def mmcli_read_one(path):
    out = strip_ansi(subprocess.check_output(
        ['mmcli', '-s', path], stderr=subprocess.DEVNULL, text=True
    ))
    def field(name):
        m = re.search(rf'{re.escape(name)}:\s*(.+)', out)
        return m.group(1).strip() if m else ''
    return {
        'index':     path.split('/')[-1],
        'number':    field('number'),
        'text':      field('text'),
        'state':     field('state'),
        'timestamp': field('timestamp'),
        'path':      path,
    }


def mmcli_delete_one(modem_idx, path):
    # Try without sudo first (active session may have polkit permission)
    r = subprocess.run(
        ['mmcli', '-m', modem_idx, f'--messaging-delete-sms={path}'],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        r = sudo_run('mmcli', '-m', modem_idx, f'--messaging-delete-sms={path}', check=False)
        if r.returncode != 0:
            die(f"could not delete SMS {path}:\n{r.stderr.strip()}")
    print(f"  Deleted: {path}")


def cmd_mmcli_list(args):
    modem_idx = mmcli_find_modem()
    paths = mmcli_list_paths(modem_idx)
    if not paths:
        print("No SMS messages found.")
        return
    print(f"Found {len(paths)} message(s):\n")
    for i, path in enumerate(paths, 1):
        msg = mmcli_read_one(path)
        print(f"── Message {i} (index {msg['index']}) {'─' * 40}")
        print(f"  From : {msg['number']}")
        print(f"  Time : {msg['timestamp']}")
        print(f"  State: {msg['state']}")
        print(f"  Text : {msg['text']}")
        print()


def cmd_mmcli_delete(args):
    modem_idx = mmcli_find_modem()
    if args.delete == 'all':
        paths = mmcli_list_paths(modem_idx)
        if not paths:
            print("No SMS messages to delete.")
            return
        print(f"Deleting {len(paths)} message(s)...")
        for path in paths:
            mmcli_delete_one(modem_idx, path)
        print("Done.")
    else:
        path = f'/org/freedesktop/ModemManager1/SMS/{args.delete}'
        mmcli_delete_one(modem_idx, path)


# ── AT mode ───────────────────────────────────────────────────────────────────

class AtPort:
    """
    Context manager that stops ModemManager, opens the AT port, and
    restarts ModemManager on exit.

    Port access requires the user to be in the 'dialout' group and the
    udev rule udev/99-wwan-at.rules to be installed (sets GROUP=dialout,
    MODE=0660 on the wwan AT device).

    Required sudoers entries: systemctl stop/start ModemManager (LTE_SERVICES).
    """

    def __init__(self, port):
        self.port = port
        self.fd = None
        self._lte_conn = None   # NM connection to restore after MM restart

    def __enter__(self):
        # Remember active LTE connection so we can restore it after MM restarts
        r = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE,STATE', 'connection', 'show', '--active'],
            capture_output=True, text=True
        )
        for line in r.stdout.splitlines():
            if ':gsm:' in line:
                self._lte_conn = line.split(':')[0]
                break

        print("Stopping ModemManager...", end=' ', flush=True)
        sudo_run('systemctl', 'stop', 'ModemManager')
        print("OK")
        time.sleep(0.5)

        try:
            self.fd = open(self.port, 'r+b', buffering=0)
        except PermissionError:
            self._restore()
            die(
                f"cannot open {self.port} — ensure you are in the 'dialout' group "
                f"and udev/99-wwan-at.rules is installed.\n"
                f"  sudo cp udev/99-wwan-at.rules /etc/udev/rules.d/\n"
                f"  sudo udevadm control --reload-rules && sudo udevadm trigger"
            )
        except OSError as e:
            self._restore()
            die(f"could not open {self.port}: {e}")

        attrs = termios.tcgetattr(self.fd)
        attrs[2] = termios.B115200
        attrs[3] &= ~(termios.ECHO | termios.ICANON)
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        return self

    def __exit__(self, *_):
        if self.fd:
            self.fd.close()
            self.fd = None
        self._restore()

    def _restore(self):
        print("Starting ModemManager...", end=' ', flush=True)
        sudo_run('systemctl', 'start', 'ModemManager', check=False)
        print("OK")
        if self._lte_conn:
            print(f"Reconnecting '{self._lte_conn}'...", end=' ', flush=True)
            time.sleep(5)   # give MM time to re-initialise the modem
            r = sudo_run('nmcli', 'connection', 'up', self._lte_conn, check=False)
            print("OK" if r.returncode == 0 else "failed (reconnect manually)")

    def cmd(self, cmd_str, wait=0.5):
        self.fd.write((cmd_str + '\r\n').encode())
        time.sleep(wait)
        raw = b''
        while select.select([self.fd], [], [], 0.3)[0]:
            raw += self.fd.read(1024)
        return raw.decode(errors='replace')


def parse_cmgl(response):
    """Parse AT+CMGL="ALL" text-mode response into list of dicts."""
    msgs = []
    lines = response.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith('+CMGL:'):
            i += 1
            continue
        # Format: +CMGL: idx,"stat","number"[,["alpha"]],"YY/MM/DD,HH:MM:SS+TZ"
        # Alpha field may be absent, empty (,,), or quoted. Use .*? to skip it
        # and anchor timestamp as the last quoted field on the line.
        rest = line[6:].strip()
        m = re.match(r'(\d+),"([^"]*)","([^"]*)".*?"([^"]*)"$', rest)
        if m:
            idx, state, number, timestamp = m.groups()
            text = lines[i + 1].strip() if i + 1 < len(lines) else ''
            msgs.append({
                'index': idx, 'state': state,
                'number': number, 'timestamp': timestamp, 'text': text,
            })
            i += 2
        else:
            i += 1
    return msgs


def cmd_at_list(args):
    with AtPort(args.at) as at:
        at.cmd('ATE0', wait=0.3)
        at.cmd('AT+CMGF=1', wait=0.3)
        response = at.cmd('AT+CMGL="ALL"', wait=1.5)

    msgs = parse_cmgl(response)
    if not msgs:
        print("\nNo SMS messages found.")
        return
    print(f"\nFound {len(msgs)} message(s):\n")
    for msg in msgs:
        print(f"── Message {msg['index']} {'─' * 49}")
        print(f"  From : {msg['number']}")
        print(f"  Time : {msg['timestamp']}")
        print(f"  State: {msg['state']}")
        print(f"  Text : {msg['text']}")
        print()


def cmd_at_delete(args):
    with AtPort(args.at) as at:
        at.cmd('ATE0', wait=0.3)
        at.cmd('AT+CMGF=1', wait=0.3)

        if args.delete == 'all':
            # flag 4 = delete all messages regardless of status
            resp = at.cmd('AT+CMGD=1,4', wait=1.0)
            if 'OK' in resp:
                print("\nAll SMS deleted.")
            else:
                print(f"\nUnexpected modem response: {repr(resp)}", file=sys.stderr)
        else:
            resp = at.cmd(f'AT+CMGD={args.delete}', wait=1.0)
            if 'OK' in resp:
                print(f"\nMessage {args.delete} deleted.")
            else:
                print(f"\nUnexpected modem response: {repr(resp)}", file=sys.stderr)


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Read and manage SMS messages from the LTE modem.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''\
examples:
  %(prog)s                          list all SMS via ModemManager (default)
  %(prog)s --delete 0               delete SMS with D-Bus index 0 (mmcli)
  %(prog)s --delete all             delete all SMS (mmcli)
  %(prog)s --at                     list all SMS via AT port
  %(prog)s --at --delete 1          delete SMS at AT storage index 1
  %(prog)s --at --delete all        delete all SMS via AT port
'''
    )
    parser.add_argument(
        '--at', nargs='?', const=AT_PORT_DEFAULT, metavar='PORT',
        help=f'use AT commands directly (default: {AT_PORT_DEFAULT}); '
             f'stops/starts ModemManager and adjusts port permissions automatically'
    )
    parser.add_argument(
        '--delete', metavar='INDEX|all',
        help='delete a specific SMS by index, or "all" to wipe every message'
    )
    args = parser.parse_args()

    at_mode = args.at is not None

    if args.delete:
        if at_mode:
            cmd_at_delete(args)
        else:
            cmd_mmcli_delete(args)
    else:
        if at_mode:
            cmd_at_list(args)
        else:
            cmd_mmcli_list(args)


if __name__ == '__main__':
    main()
