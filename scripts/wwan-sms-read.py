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
DEFAULT_CONNECTION = 'Orange'   # NM connection name used to bring the SIM online
REGISTER_TIMEOUT = 15          # seconds to wait for network registration after connecting
PENDING_SMS_WAIT = 10           # seconds to wait for pending SMS to arrive once online


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


# ── shared: filtering, code extraction, clipboard/notify ───────────────────────

CODE_KEYWORD_RE = re.compile(r'c[oó]digo|code|clave|pin|otp', re.IGNORECASE)
# token must contain at least one digit, so plain words near the keyword
# ("confirmacion", "aqui"...) are never mistaken for the actual code
CODE_TOKEN_RE = re.compile(r'\b(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,8}\b')
CODE_SEARCH_WINDOW = 40   # chars scanned after the keyword for the code token


def compile_filter(pattern):
    return re.compile(pattern, re.IGNORECASE) if pattern else None


def matches_filters(msg, from_re, grep_re):
    if from_re and not from_re.search(msg['number']):
        return False
    if grep_re and not grep_re.search(msg['text']):
        return False
    return True


def extract_code(text):
    km = CODE_KEYWORD_RE.search(text)
    if not km:
        return None
    tail = text[km.end():km.end() + CODE_SEARCH_WINDOW]
    tm = CODE_TOKEN_RE.search(tail)
    return tm.group(0) if tm else None


def copy_to_clipboard(text):
    for cmd in (['wl-copy'], ['xclip', '-selection', 'clipboard']):
        try:
            subprocess.run(cmd, input=text.encode(), check=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return False


def notify_send(title, body):
    try:
        subprocess.run(['notify-send', title, body], check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


def show_codes(msgs):
    found = False
    for msg in msgs:
        code = extract_code(msg['text'])
        if not code:
            continue
        found = True
        print(f"Código detectado (de {msg['number']}): {code}")
        if copy_to_clipboard(code):
            print("  → copiado al portapapeles")
        notify_send("SMS: código recibido", f"{msg['number']}: {code}")
    if not found:
        print("No se detectó ningún código de confirmación en los mensajes mostrados.")


def print_messages(msgs):
    if not msgs:
        print("No SMS messages found.")
        return
    print(f"Found {len(msgs)} message(s):\n")
    for i, msg in enumerate(msgs, 1):
        print(f"── Message {i} (index {msg['index']}) {'─' * 40}")
        print(f"  From : {msg['number']}")
        print(f"  Time : {msg['timestamp']}")
        print(f"  State: {msg['state']}")
        print(f"  Text : {msg['text']}")
        print()


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


def mmcli_modem_status(modem_idx):
    out = subprocess.check_output(
        ['mmcli', '-m', modem_idx, '-K'], stderr=subprocess.DEVNULL, text=True
    )
    status = {}
    for line in out.splitlines():
        key, _, val = line.partition(':')
        status[key.strip()] = val.strip()
    return status


def mmcli_is_registered(modem_idx):
    status = mmcli_modem_status(modem_idx)
    return (status.get('modem.generic.state') == 'connected'
            and status.get('modem.3gpp.registration-state') in ('home', 'roaming'))


def prompt_yes_no(question, default=False, auto=None):
    if auto is not None:
        print(f"{question} → {'s' if auto else 'n'} (automático, --yes)")
        return auto
    hint = '[S/n]' if default else '[s/N]'
    try:
        ans = input(f"{question} {hint}: ").strip().lower()
    except EOFError:
        ans = ''
    if not ans:
        return default
    return ans in ('s', 'si', 'sí', 'y', 'yes')


def mmcli_connect(modem_idx, connection):
    print(f"Conectando SIM a la red (nmcli connection up {connection})...", end=' ', flush=True)
    r = sudo_run('nmcli', 'connection', 'up', connection, check=False)
    if r.returncode != 0:
        print("FALLO")
        print(f"  {r.stderr.strip()}", file=sys.stderr)
        return False
    for _ in range(REGISTER_TIMEOUT):
        if mmcli_is_registered(modem_idx):
            print("OK (registrada en red)")
            return True
        time.sleep(1)
    print("iniciada, pero no se confirmó el registro en red a tiempo")
    return True


def mmcli_wait_pending_sms(seconds=PENDING_SMS_WAIT):
    print(f"Esperando SMS pendientes ({seconds}s): ", end='', flush=True)
    for remaining in range(seconds, 0, -1):
        print(f"{remaining}.. ", end='', flush=True)
        time.sleep(1)
    print("listo")


def mmcli_disconnect(connection):
    print(f"Desconectando SIM (nmcli connection down {connection})...", end=' ', flush=True)
    r = sudo_run('nmcli', 'connection', 'down', connection, check=False)
    print("OK" if r.returncode == 0 else "FALLO")


def cmd_mmcli_list(args):
    modem_idx = mmcli_find_modem()
    from_re = compile_filter(args.from_number)
    grep_re = compile_filter(args.grep)
    auto = True if args.yes else None

    baseline_paths = set(mmcli_list_paths(modem_idx)) if args.watch else None

    connected_by_us = False
    registered = mmcli_is_registered(modem_idx)
    if not registered:
        print(
            "Aviso: la SIM está desconectada de la red — solo se mostrarán los\n"
            "SMS que ya estuvieran almacenados en la tarjeta.\n"
        )
        if prompt_yes_no("¿Conectar la SIM a la red para comprobar mensajes pendientes?", auto=auto):
            connected_by_us = mmcli_connect(modem_idx, args.connection)
            registered = connected_by_us
        print()

    if args.watch:
        if not registered:
            print("No se pudo conectar; no es posible esperar SMS nuevos.\n")
            paths = []
        else:
            mmcli_wait_pending_sms(args.wait)
            paths = [p for p in mmcli_list_paths(modem_idx) if p not in baseline_paths]
            if not paths:
                print("\nNo han llegado mensajes nuevos.\n")
    else:
        if connected_by_us:
            mmcli_wait_pending_sms(args.wait)
        paths = mmcli_list_paths(modem_idx)

    msgs = [mmcli_read_one(p) for p in paths]
    msgs = [m for m in msgs if matches_filters(m, from_re, grep_re)]

    print_messages(msgs)

    if args.code:
        show_codes(msgs)

    if args.delete_after_read and msgs:
        print(f"Borrando {len(msgs)} mensaje(s) mostrado(s)...")
        for m in msgs:
            mmcli_delete_one(modem_idx, m['path'])

    if connected_by_us:
        if args.keep_online is not None:
            if args.keep_online > 0:
                print(f"\nManteniendo la SIM conectada {args.keep_online}s antes de desconectar...")
                time.sleep(args.keep_online)
            mmcli_disconnect(args.connection)
        else:
            if prompt_yes_no("¿Desconectar la SIM ahora?", default=True, auto=auto):
                mmcli_disconnect(args.connection)
            else:
                print(f"Para desconectarla manualmente más tarde:\n  sudo nmcli connection down {args.connection}")


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
    from_re = compile_filter(args.from_number)
    grep_re = compile_filter(args.grep)

    with AtPort(args.at) as at:
        at.cmd('ATE0', wait=0.3)
        at.cmd('AT+CMGF=1', wait=0.3)
        response = at.cmd('AT+CMGL="ALL"', wait=1.5)

        msgs = parse_cmgl(response)
        msgs = [m for m in msgs if matches_filters(m, from_re, grep_re)]

        print()
        print_messages(msgs)

        if args.code:
            show_codes(msgs)

        if args.delete_after_read and msgs:
            print(f"Borrando {len(msgs)} mensaje(s) mostrado(s)...")
            for m in msgs:
                resp = at.cmd(f"AT+CMGD={m['index']}", wait=1.0)
                status = "OK" if 'OK' in resp else f"FALLO: {resp.strip()}"
                print(f"  Mensaje {m['index']}: {status}")


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
  %(prog)s --from openbank --code   connect if needed, show only Openbank SMS, extract the code
  %(prog)s --watch --code -y        connect without asking, wait for a new SMS, show its code, disconnect
  %(prog)s --grep "(?i)codigo" --delete-after-read
                                     show messages containing "codigo" and delete them once read
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
    parser.add_argument(
        '--connection', default=DEFAULT_CONNECTION, metavar='NAME',
        help=f'NM connection name used to bring the SIM online (mmcli mode only, default: {DEFAULT_CONNECTION})'
    )
    parser.add_argument(
        '--from', dest='from_number', metavar='PATTERN',
        help='only show messages whose sender matches PATTERN (regex, case-insensitive)'
    )
    parser.add_argument(
        '--grep', metavar='PATTERN',
        help='only show messages whose text matches PATTERN (regex, case-insensitive)'
    )
    parser.add_argument(
        '--code', action='store_true',
        help='extract and highlight confirmation codes from the shown messages; '
             'copies the first one found to the clipboard and sends a desktop notification if available'
    )
    parser.add_argument(
        '--delete-after-read', action='store_true',
        help='delete every message shown (after filtering) once printed'
    )
    parser.add_argument(
        '--watch', action='store_true',
        help='(mmcli mode only) connect if needed, wait --wait seconds, and show only messages '
             'that arrived during the wait instead of the full mailbox'
    )
    parser.add_argument(
        '--wait', type=int, default=PENDING_SMS_WAIT, metavar='SECONDS',
        help=f'(mmcli mode only) seconds to wait for pending/new SMS after connecting or with '
             f'--watch (default: {PENDING_SMS_WAIT})'
    )
    parser.add_argument(
        '--keep-online', type=int, metavar='SECONDS',
        help='(mmcli mode only) if the script had to connect the SIM, wait SECONDS then disconnect '
             'automatically instead of asking (0 = disconnect immediately)'
    )
    parser.add_argument(
        '-y', '--yes', action='store_true',
        help='assume yes to all interactive prompts (connect / disconnect), for non-interactive use'
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
