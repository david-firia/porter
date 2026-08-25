#!/usr/bin/env python3
"""porter - a serial terminal with a live device picker.

Cycle between USB serial devices without hunting for COM numbers.  Devices are
tracked by stable USB identity (vid:pid:serial), so a board that comes back on a
different port is still recognised as the same board.

Single file; pyserial is the only dependency.

    pip install pyserial
    python porter.py
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import faulthandler
import fnmatch
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    sys.exit("porter requires pyserial:  pip install pyserial")

WINDOWS = sys.platform == "win32"

if WINDOWS:
    import ctypes
    import msvcrt
else:
    import select
    import termios
    import tty


# --------------------------------------------------------------------------
# ANSI
# --------------------------------------------------------------------------

CSI = "\x1b["

ALT_ON = CSI + "?1049h"
ALT_OFF = CSI + "?1049l"
CUR_HIDE = CSI + "?25l"
CUR_SHOW = CSI + "?25h"
HOME = CSI + "H"
CLEAR_EOL = CSI + "K"
CLEAR_EOS = CSI + "J"
CLEAR_SCREEN = CSI + "2J" + CSI + "H"

SGR0 = CSI + "0m"
BOLD = CSI + "1m"
DIM = CSI + "2m"
REV = CSI + "7m"
CYAN = CSI + "36m"
GREEN = CSI + "32m"
YELLOW = CSI + "33m"
RED = CSI + "31m"


# --------------------------------------------------------------------------
# Themes
# --------------------------------------------------------------------------
#
# Sunlight eats every distinction the default palette is built out of: dim
# greys, mid-tone colours, the gap between 36m and 32m.  A high-contrast theme
# spends all of that on legibility instead -- one foreground, one background,
# and bold or reverse for the few things that still have to stand out.
#
# Call sites ask for a *role* (`T.warn`), never a colour, so the theme is the
# only thing that decides what a warning looks like.


@dataclass(frozen=True)
class Theme:
    """A palette by role, plus the screen colours it wants the terminal set to."""

    name: str
    fg: str            # terminal default colours, as OSC 10/11 wants them;
    bg: str            # empty means "leave the user's own scheme alone"
    base: str          # SGR that re-establishes fg/bg after a reset
    bold: str
    muted: str         # counts, key hints, paths -- present but secondary
    sel: str           # the selected row
    alias: str         # a device the config has a name for
    ok: str
    warn: str
    err: str
    strip_sgr: bool    # flatten the device's own colours into the theme's

    @property
    def reset(self) -> str:
        """End a styled run without falling back to the terminal's colours."""
        return SGR0 + self.base


# Both high-contrast themes spend their roles the same way; only the two
# screen colours differ.
_HC_ROLES = dict(bold=BOLD, muted="", sel=REV, alias=BOLD, ok=BOLD, warn=BOLD,
                 err=REV, strip_sgr=True)

THEMES = {
    "default": Theme("default", fg="", bg="", base="",
                     bold=BOLD, muted=DIM, sel=REV, alias=CYAN, ok=GREEN,
                     warn=YELLOW, err=RED, strip_sgr=False),
    "contrast-dark": Theme("contrast-dark", fg="#ffffff", bg="#000000",
                           base=CSI + "0;97;40m", **_HC_ROLES),
    "contrast-light": Theme("contrast-light", fg="#000000", bg="#ffffff",
                            base=CSI + "0;30;107m", **_HC_ROLES),
}

# OSC 10/11 set the terminal's *own* default colours, so a switch repaints the
# scrollback that is already on screen; 110/111 hand them back.
_OSC_SET = "\x1b]10;{}\x07\x1b]11;{}\x07"
_OSC_RESET = "\x1b]110\x07\x1b]111\x07"

T = THEMES["default"]


_OUT_LOCK = threading.Lock()
_LAST_BEAT = [time.monotonic()]


def _beat() -> None:
    """Mark the main loop as alive, for the --debug watchdog."""
    _LAST_BEAT[0] = time.monotonic()


def _start_watchdog(path: str, stall: float = 8.0) -> None:
    """Dump every thread's stack if the main loop stops ticking.

    The failure this exists for is a native call blocking the main thread,
    where there is nothing left to print the diagnosis from.
    """
    log = open(path, "a", buffering=1, encoding="utf-8")
    log.write(f"\n=== porter started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    faulthandler.enable(file=log, all_threads=True)

    def run():
        while True:
            time.sleep(1.0)
            behind = time.monotonic() - _LAST_BEAT[0]
            if behind > stall:
                log.write(f"\n=== main loop stalled {behind:.1f}s at "
                          f"{time.strftime('%H:%M:%S')} ===\n")
                faulthandler.dump_traceback(file=log, all_threads=True)
                _LAST_BEAT[0] = time.monotonic()

    threading.Thread(target=run, daemon=True, name="watchdog").start()


def w(text: str) -> None:
    """Write UI text to the terminal."""
    with _OUT_LOCK:
        sys.stdout.write(text)
        sys.stdout.flush()


class _SGRStrip:
    """Drop SGR sequences from a stream of device bytes.

    A high-contrast theme is worthless if the device paints its own dim blue
    over the top of it.  Only colour and attribute sequences go; cursor motion
    and erases pass through, so a full-screen program on the far end still
    works -- it just arrives monochrome.

    A sequence split across two reads is held back until it completes, which
    is why this carries state and lives behind the output lock.
    """

    MAX_HOLD = 64          # a truecolour fg+bg run is ~40; past this it is data

    def __init__(self) -> None:
        self._held = bytearray()

    def flush(self) -> bytes:
        """Release a half-arrived sequence unchanged, for leaving strip mode."""
        held, self._held = bytes(self._held), bytearray()
        return held

    def feed(self, data: bytes) -> bytes:
        buf = self.flush() + data
        out = bytearray()
        i = 0
        while True:
            j = buf.find(0x1B, i)
            if j < 0:
                return bytes(out + buf[i:])
            out += buf[i:j]
            if j + 1 >= len(buf):
                self._held = bytearray(buf[j:])
                return bytes(out)
            if buf[j + 1] != 0x5B:              # not CSI, so not ours to touch
                out += buf[j:j + 2]
                i = j + 2
                continue
            k = j + 2                           # scan to the final byte
            while k < len(buf) and not 0x40 <= buf[k] <= 0x7E:
                k += 1
            if k == len(buf):
                if k - j > self.MAX_HOLD:       # runaway: it was never a
                    return bytes(out + buf[j:])  # sequence, stop swallowing it
                self._held = bytearray(buf[j:])
                return bytes(out)
            if buf[k] != 0x6D:                  # 'm' is the only one we eat
                out += buf[j:k + 1]
            i = k + 1


_SGR = _SGRStrip()


def w_bytes(data: bytes) -> None:
    """Write device bytes straight through -- never via the text layer, which
    would rewrite newlines on Windows and corrupt binary output."""
    with _OUT_LOCK:
        data = _SGR.feed(data) if T.strip_sgr else _SGR.flush() + data
        if not data:
            return
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def note(text: str, style: str | None = None) -> None:
    """Write a porter status line.  Raw mode, so newlines must be explicit."""
    w(f"\r\n{T.muted if style is None else style}[porter] {text}{T.reset}\r\n")


def set_theme(name: str) -> None:
    """Switch palette, and tell the terminal which default colours to paint."""
    global T
    had_colours = bool(T.fg)
    T = THEMES[name]
    if T.fg:
        w(_OSC_SET.format(T.fg, T.bg) + T.reset)
    elif had_colours:
        w(_OSC_RESET + T.reset)


def next_theme() -> str:
    """Step to the next theme in the table.  Returns the name now in force."""
    names = list(THEMES)
    set_theme(names[(names.index(T.name) + 1) % len(names)])
    return T.name


@contextlib.contextmanager
def alt_screen():
    """Run the picker on the alternate buffer so session scrollback survives."""
    w(ALT_ON + T.reset + CLEAR_SCREEN + CUR_HIDE)
    try:
        yield
    finally:
        w(CUR_SHOW + ALT_OFF)


# --------------------------------------------------------------------------
# Raw console
# --------------------------------------------------------------------------

_STD_INPUT = 0xFFFFFFF6   # (DWORD)-10
_STD_OUTPUT = 0xFFFFFFF5  # (DWORD)-11
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


class RawTerm:
    """Put the console in raw mode and restore it whatever happens.

    Clearing ENABLE_PROCESSED_INPUT on Windows is what lets ctrl-c reach the
    device as a 0x03 byte instead of raising KeyboardInterrupt, and
    ENABLE_VIRTUAL_TERMINAL_INPUT makes Windows deliver arrow keys as ANSI
    sequences -- so one key parser covers both platforms.
    """

    def __init__(self) -> None:
        self._saved_in = None
        self._saved_out = None

    def __enter__(self) -> "RawTerm":
        if WINDOWS:
            k = ctypes.windll.kernel32
            # Declare signatures: a HANDLE is pointer-sized, and ctypes would
            # otherwise truncate it to a 32-bit int on 64-bit Windows.
            k.GetStdHandle.restype = ctypes.c_void_p
            k.GetStdHandle.argtypes = [ctypes.c_uint32]
            k.GetConsoleMode.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_uint32)]
            k.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k.GetNumberOfConsoleInputEvents.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            k.ReadConsoleInputW.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32)]
            global _WIN_HIN
            self._hin = k.GetStdHandle(_STD_INPUT)
            _WIN_HIN = self._hin
            self._hout = k.GetStdHandle(_STD_OUTPUT)
            mode_in = ctypes.c_uint32()
            mode_out = ctypes.c_uint32()
            k.GetConsoleMode(self._hin, ctypes.byref(mode_in))
            k.GetConsoleMode(self._hout, ctypes.byref(mode_out))
            self._saved_in = mode_in.value
            self._saved_out = mode_out.value

            raw = mode_in.value & ~(
                _ENABLE_PROCESSED_INPUT | _ENABLE_LINE_INPUT | _ENABLE_ECHO_INPUT
            )
            if not k.SetConsoleMode(self._hin, raw | _ENABLE_VIRTUAL_TERMINAL_INPUT):
                # Legacy conhost: fall back to raw without VT input.  _read_raw
                # translates the 0x00/0xe0 special-key prefixes in that case.
                k.SetConsoleMode(self._hin, raw)
            k.SetConsoleMode(
                self._hout, mode_out.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
            )
        else:
            self._fd = sys.stdin.fileno()
            self._saved_in = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        return self

    def __exit__(self, *exc) -> bool:
        if self._saved_in is None:
            return False
        if WINDOWS:
            k = ctypes.windll.kernel32
            k.SetConsoleMode(self._hin, self._saved_in)
            k.SetConsoleMode(self._hout, self._saved_out)
        else:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_in)
        return False


# Legacy-conhost special keys, mapped onto the ANSI sequences we parse anyway.
# Windows console input.
#
# msvcrt.kbhit()/getwch() is not safe here.  kbhit() promises exactly ONE
# character; the prefix path used to call getwch() a second time for the
# 0x00/0xe0 lead byte, which blocks forever when no second record follows --
# a dead main thread, so no keyboard, no port polling and no way to quit.
# Reading INPUT_RECORDs directly can never block: we ask how many are queued
# and read only that many.

_KEY_EVENT = 0x0001

# Legacy fallback for when ENABLE_VIRTUAL_TERMINAL_INPUT could not be set:
# synthesise the VT sequence the parser already understands.
_VK_SEQ = {0x26: b"\x1b[A", 0x28: b"\x1b[B", 0x25: b"\x1b[D", 0x27: b"\x1b[C",
           0x24: b"\x1b[H", 0x23: b"\x1b[F", 0x2E: b"\x1b[3~"}

if WINDOWS:

    class _KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [("bKeyDown", ctypes.c_int),
                    ("wRepeatCount", ctypes.c_ushort),
                    ("wVirtualKeyCode", ctypes.c_ushort),
                    ("wVirtualScanCode", ctypes.c_ushort),
                    ("UnicodeChar", ctypes.c_wchar),
                    ("dwControlKeyState", ctypes.c_uint32)]

    class _EVENT_UNION(ctypes.Union):
        _fields_ = [("KeyEvent", _KEY_EVENT_RECORD),
                    ("_pad", ctypes.c_byte * 16)]

    class _INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", ctypes.c_ushort), ("Event", _EVENT_UNION)]


_WIN_HIN = None          # console input handle, set by RawTerm

# INPUT_RECORD is 20 bytes on x86 and x64 alike.  If the layout ever disagrees,
# fall back to msvcrt rather than silently misparse every keystroke.
_win_console_ok = (not WINDOWS) or ctypes.sizeof(_INPUT_RECORD) == 20


def _win_read_console() -> bytes:
    """Drain queued key events.  Never blocks; returns b'' when idle."""
    global _win_console_ok
    k = ctypes.windll.kernel32
    pending = ctypes.c_uint32()
    if not k.GetNumberOfConsoleInputEvents(_WIN_HIN, ctypes.byref(pending)):
        _win_console_ok = False
        return b""
    if pending.value == 0:
        return b""

    count = min(pending.value, 128)
    recs = (_INPUT_RECORD * count)()
    got = ctypes.c_uint32()
    if not k.ReadConsoleInputW(_WIN_HIN, recs, count, ctypes.byref(got)):
        _win_console_ok = False
        return b""

    out = bytearray()
    for i in range(got.value):
        rec = recs[i]
        # Resize, focus, menu and mouse records are discarded here.  Letting
        # them reach the old kbhit() loop is what made it wedge.
        if rec.EventType != _KEY_EVENT:
            continue
        key = rec.Event.KeyEvent
        if not key.bKeyDown:
            continue
        ch = key.UnicodeChar
        if ch and ch != "\x00":
            out += ch.encode("utf-8", "replace")
        else:
            out += _VK_SEQ.get(key.wVirtualKeyCode, b"")
    return bytes(out)


def _read_raw(timeout: float) -> bytes:
    """Return whatever keyboard input is available, or b'' after `timeout`."""
    if WINDOWS:
        deadline = time.monotonic() + timeout
        while True:
            data = _win_read_console() if _win_console_ok else _win_read_msvcrt()
            if data or time.monotonic() >= deadline:
                return data
            time.sleep(0.004)

    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return b""
    try:
        return os.read(sys.stdin.fileno(), 1024)
    except OSError:
        return b""


def _win_read_msvcrt() -> bytes:
    """Fallback if the console API is unavailable.  Guarded: the lead-byte
    read only happens when a second character is genuinely queued."""
    legacy = {"H": b"\x1b[A", "P": b"\x1b[B", "K": b"\x1b[D", "M": b"\x1b[C",
              "G": b"\x1b[H", "O": b"\x1b[F", "S": b"\x1b[3~"}
    out = bytearray()
    while msvcrt.kbhit():
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            if not msvcrt.kbhit():
                continue                      # lone prefix: drop, never block
            out += legacy.get(msvcrt.getwch(), b"")
        else:
            out += ch.encode("utf-8", "replace")
    return bytes(out)


_ARROWS = {0x41: "UP", 0x42: "DOWN", 0x43: "RIGHT", 0x44: "LEFT",
           0x48: "HOME", 0x46: "END"}
_pending = bytearray()


def read_key(timeout: float = 0.4):
    """Return a key token ('UP', 'ENTER', 'q', ...) or None on timeout."""
    if not _pending:
        _pending.extend(_read_raw(timeout))
    if not _pending:
        return None

    if _pending[0] == 0x1B:
        if len(_pending) == 1:
            _pending.extend(_read_raw(0.03))
        if len(_pending) == 1:
            del _pending[0]
            return "ESC"
        if _pending[1] in (0x5B, 0x4F):  # CSI / SS3
            i = 2
            while i < len(_pending) and not 0x40 <= _pending[i] <= 0x7E:
                i += 1
            if i >= len(_pending):
                _pending.clear()
                return None
            final = _pending[i]
            del _pending[: i + 1]
            return _ARROWS.get(final, "UNKNOWN")
        del _pending[:2]
        return "ESC"

    b = _pending[0]
    del _pending[0]
    if b in (0x0D, 0x0A):
        return "ENTER"
    if b == 0x09:
        return "TAB"
    if b in (0x08, 0x7F):
        return "BACK"
    if b < 0x20:
        return f"CTRL-{chr(b + 64)}"
    return chr(b)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

GLOBAL = "porter"
DEFAULT_BAUD = 115200
BAUD_CYCLE = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]

STARTER_CONFIG = """\
# porter config -- tio-style INI.
#
# [porter] holds defaults.  Every other section is a device alias, matched
# against a device's stable USB identity.  Run `porter --list` to see the ids
# of what is currently plugged in, or press `a` in the picker to append a stub.

[porter]
baudrate = 115200
# exclude = COM1, *Bluetooth*, /dev/ttyS*
#
# Start in a high-contrast palette instead of the default colours -- `H`
# cycles the three at any time.  default | contrast-dark | contrast-light
# theme = contrast-dark

# An `id` may be a full vid:pid:serial for one specific board...
#
# [codebot-3]
# id = 239a:80f4:DF6202B3184E3033
# baudrate = 115200
#
# ...or just vid:pid to match any board of that type.
#
# [ftdi]
# id = 0403:6001
# baudrate = 921600
# dtr = true
# rts = false
"""


def config_path(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    env = os.environ.get("PORTER_CONFIG")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "porter" / "config"
    if WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "porter" / "config"
    return Path.home() / ".config" / "porter" / "config"


def load_config(path: Path):
    """Return (config, warning-or-None)."""
    cfg = configparser.ConfigParser(interpolation=None)
    if not path.exists():
        return cfg, None
    try:
        cfg.read(path, encoding="utf-8")
    except configparser.Error as exc:
        return cfg, f"{path}: {exc}"
    return cfg, None


def _global_get(cfg, key, fallback=None):
    if cfg.has_section(GLOBAL):
        return cfg[GLOBAL].get(key, fallback)
    return fallback


def _as_bool(value, fallback=None):
    if value is None:
        return fallback
    return value.strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------


@dataclass
class Device:
    port: str
    key: str          # stable identity: vid:pid:serial
    label: str
    baud: int
    vid: int | None = None
    pid: int | None = None
    alias: str | None = None
    dtr: bool | None = None
    rts: bool | None = None

    @property
    def ident(self) -> str:
        if self.vid is not None and self.pid is not None:
            return f"{self.vid:04x}:{self.pid:04x}"
        return "-"


SETTLE = 0.6        # let a freshly-appeared device's driver attach
SCANNER = None


class PortScanner(threading.Thread):
    """Enumerate serial ports on a worker thread.

    On Windows comports() is a full SetupAPI device-tree walk that can stall
    for seconds while a driver attaches -- which is exactly when we call it
    most, right after a replug.  The UI reads a cached snapshot and never
    blocks.  Also tracks how long each device has been continuously present,
    so auto-reconnect can wait for the driver instead of racing it.
    """

    def __init__(self, interval: float = 0.35) -> None:
        super().__init__(daemon=True, name="port-scanner")
        self.interval = interval
        self._lock = threading.Lock()
        self._ports: list = []
        self._since: dict = {}
        self.ready = threading.Event()

    def run(self) -> None:
        since: dict = {}
        while True:
            try:
                ports = list(list_ports.comports())
            except Exception:
                ports = []
            now = time.monotonic()
            keys = {_identity(p) for p in ports}
            for gone in [k for k in since if k not in keys]:
                del since[gone]
            for k in keys:
                since.setdefault(k, now)
            with self._lock:
                self._ports, self._since = ports, dict(since)
            self.ready.set()
            time.sleep(self.interval)

    def ports(self) -> list:
        with self._lock:
            return list(self._ports)

    def age(self, key: str) -> float:
        """Seconds this device has been continuously present, or -1.0."""
        with self._lock:
            first = self._since.get(key)
        return -1.0 if first is None else time.monotonic() - first


def _scan_ports() -> list:
    if SCANNER is not None:
        return SCANNER.ports()
    return list(list_ports.comports())


def _identity(p) -> str:
    """A key that survives replugging into a different port."""
    if p.vid is None or p.pid is None:
        return p.device
    base = f"{p.vid:04x}:{p.pid:04x}"
    if p.serial_number:
        return f"{base}:{p.serial_number}"
    if p.location:
        # No serial number: two identical cables stay distinct by hub position.
        return f"{base}@{p.location}"
    return f"{base}:{p.device}"


def _match_alias(cfg, key: str):
    """Exact id match wins; otherwise the longest vid:pid prefix."""
    lowered = key.lower()
    best_name, best_pat = None, ""
    for name in cfg.sections():
        if name == GLOBAL:
            continue
        pat = cfg[name].get("id", "").strip().lower()
        if not pat:
            continue
        if lowered == pat:
            return name
        if lowered.startswith(pat + ":") or lowered.startswith(pat + "@"):
            if len(pat) > len(best_pat):
                best_name, best_pat = name, pat
    return best_name


def _excluded(cfg, port: str, key: str, desc: str) -> bool:
    raw = _global_get(cfg, "exclude", "") or ""
    for pat in (p.strip() for p in raw.split(",")):
        if not pat:
            continue
        for field in (port, key, desc):
            if fnmatch.fnmatch(field.lower(), pat.lower()):
                return True
    return False


def enumerate_devices(cfg, overrides: dict, cli_baud: int | None,
                      show_all: bool = False) -> list[Device]:
    show_all = show_all or _as_bool(_global_get(cfg, "show_all"), False)
    default_baud = int(_global_get(cfg, "baudrate", DEFAULT_BAUD) or DEFAULT_BAUD)
    devices = []
    for p in _scan_ports():
        key = _identity(p)
        desc = p.description or p.device
        if _excluded(cfg, p.device, key, desc):
            continue

        alias = _match_alias(cfg, key)

        # Ports with no vid:pid are not USB devices: motherboard COM1/COM2,
        # Bluetooth SPP links, virtual sniffer bridges.  Hide them -- but never
        # one the user has explicitly aliased, which is how a real RS-232 port
        # stays in the list.
        if p.vid is None and alias is None and not show_all:
            continue

        section = cfg[alias] if alias else None

        # override (b key) > --baud > alias > [porter] > built-in
        if key in overrides:
            baud = overrides[key]
        elif cli_baud is not None:
            baud = cli_baud
        elif section is not None and section.get("baudrate"):
            baud = int(section["baudrate"])
        else:
            baud = default_baud

        devices.append(Device(
            port=p.device,
            key=key,
            label=alias or desc,
            baud=baud,
            vid=p.vid,
            pid=p.pid,
            alias=alias,
            dtr=_as_bool(section.get("dtr") if section else None),
            rts=_as_bool(section.get("rts") if section else None),
        ))

    # Aliased devices first, then by label -- a stable order that does not
    # reshuffle under the cursor when COM numbers change.
    devices.sort(key=lambda d: (d.alias is None, d.label.lower(), d.key))
    return devices


def _valid_alias(name: str, cfg, current: str | None = None) -> str | None:
    """Return a complaint about `name`, or None if it is usable."""
    name = name.strip()
    if not name:
        return "name cannot be empty"
    if any(c in name for c in "[]\r\n"):
        return "name cannot contain [ ] or newlines"
    if name == GLOBAL:
        return f"'{GLOBAL}' is reserved"
    if cfg.has_section(name) and name != current:
        return f"'{name}' is already used"
    return None


def add_alias(path: Path, name: str, dev: Device) -> None:
    """Append a new alias.  Appending rather than rewriting keeps the
    comments in the config file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n[{name}]\nid = {dev.key}\nbaudrate = {dev.baud}\n")


def rename_alias(path: Path, old: str, new: str) -> bool:
    """Retitle an existing section by editing just its header line."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    header = re.compile(r"^\[" + re.escape(old) + r"\][ \t]*$", re.M)
    if not header.search(text):
        return False
    path.write_text(header.sub(f"[{new}]", text, count=1), encoding="utf-8")
    return True


# --------------------------------------------------------------------------
# Picker
# --------------------------------------------------------------------------

POLL = 0.4
SPINNER = "|/-\\"
BACKLOG_MAX = 256 * 1024


class _Cancel:
    """Sentinel: leave the picker without disturbing the live session."""


CANCEL = _Cancel()


def _text_prompt(draw, label: str, initial: str = "") -> str | None:
    """Modal one-line input drawn under the device list.  None if cancelled."""
    buf = list(initial)
    while True:
        draw(label + "".join(buf) + "_")
        _beat()
        key = read_key(POLL)
        if key is None:
            continue
        if key == "ENTER":
            return "".join(buf).strip()
        if key in ("ESC", "CTRL-C"):
            return None
        if key == "BACK":
            if buf:
                buf.pop()
        elif len(key) == 1 and key.isprintable():
            buf.append(key)


def _render(devs, sel, fresh, waiting_label, spin, msg, cfg_path,
            resumable=False, prompt=None) -> None:
    width = max(40, shutil.get_terminal_size((100, 30)).columns)
    rows = []

    count = f"{len(devs)} device{'' if len(devs) == 1 else 's'}"
    head = " porter"
    rows.append(T.bold + head + T.reset + T.muted
                + count.rjust(max(1, width - len(head) - 2)) + T.reset)
    rows.append("")

    if not devs:
        rows.append(f"   {T.muted}no serial devices - plug one in{T.reset}")
    for i, d in enumerate(devs):
        marker = ">" if i == sel else " "
        num = str(i + 1) if i < 9 else " "
        plain = (f" {marker} {num}  {d.label[:22]:<22} {d.port[:14]:<14} "
                 f"{d.ident:<10} {d.baud:>7}")
        if d.key in fresh:
            plain += "  +new"
        if i == sel:
            rows.append(T.sel + plain.ljust(width - 1) + T.reset)
        elif d.alias:
            rows.append(T.alias + plain + T.reset)
        else:
            rows.append(plain)

    rows.append("")
    if waiting_label:
        rows.append(f" {T.warn}{spin} waiting for {waiting_label}"
                    f"{T.reset}{T.muted}  (any key to cancel){T.reset}")
    if msg:
        rows.append(f" {T.ok}{msg}{T.reset}")
    rows.append("")

    if prompt is not None:
        rows.append(f" {T.bold}{prompt}{T.reset}")
        rows.append(T.muted + " enter save  .  esc cancel" + T.reset)
    else:
        rows.append(T.muted + " j/k or arrows select  .  enter connect"
                    "  .  1-9 jump  .  b baud  .  a name" + T.reset)
        keys = " H high contrast"
        keys += "  .  esc resume  .  q quit" if resumable else "  .  q quit"
        rows.append(T.muted + keys + T.reset)
        rows.append(T.muted + f" aliases: {cfg_path}" + T.reset)

    w(T.reset + HOME + (CLEAR_EOL + "\r\n").join(rows) + CLEAR_EOL + CLEAR_EOS)


def picker(cfg, cfg_path: Path, overrides: dict, cli_baud: int | None,
           preselect: str | None, waiting_for: str | None,
           resumable: bool = False, show_all: bool = False):
    """Live device list.

    Returns a Device to connect to, CANCEL to go back to the live session,
    or None to quit.
    """
    devs = enumerate_devices(cfg, overrides, cli_baud, show_all)
    known = {d.key for d in devs}
    fresh: set[str] = set()
    msg = ""
    spin = 0
    dirty = True

    sel = 0
    if preselect:
        for i, d in enumerate(devs):
            if d.key == preselect:
                sel = i
                break

    while True:
        if waiting_for:
            for d in devs:
                if d.key != waiting_for:
                    continue
                # Do not pounce the instant it enumerates -- see _open_port.
                if SCANNER is None or SCANNER.age(d.key) >= SETTLE:
                    return d

        if dirty:
            label = _waiting_label(cfg, waiting_for) if waiting_for else None
            _render(devs, sel, fresh, label, SPINNER[spin % len(SPINNER)],
                    msg, cfg_path, resumable)
            dirty = False

        _beat()
        key = read_key(POLL)

        if key is not None:
            if waiting_for:               # any key cancels the auto-reconnect
                waiting_for = None
                dirty = True
            if fresh or msg:              # the +new tag has served its purpose
                fresh, msg = set(), ""
                dirty = True

        if key is None:
            spin += 1
            new_devs = enumerate_devices(cfg, overrides, cli_baud, show_all)
            new_keys = {d.key for d in new_devs}
            if new_keys != known:
                appeared = new_keys - known
                fresh |= appeared
                fresh &= new_keys
                anchor = devs[sel].key if devs and sel < len(devs) else None
                devs = new_devs
                if appeared:
                    # Jump to whatever was just plugged in: plug, enter, done.
                    first = next(d for d in devs if d.key in appeared)
                    sel = devs.index(first)
                elif anchor:
                    sel = next((i for i, d in enumerate(devs)
                                if d.key == anchor), 0)
                sel = max(0, min(sel, len(devs) - 1)) if devs else 0
                known = new_keys
                msg = ""
                dirty = True
            elif waiting_for:
                dirty = True              # keep the spinner moving
            continue

        if key in ("q", "CTRL-C"):
            return None
        if key == "ESC":
            # Escape means "never mind" when there is a session to go back to,
            # and only means quit when there is nothing behind the picker.
            return CANCEL if resumable else None
        if key in ("DOWN", "j", "TAB") and devs:
            sel = (sel + 1) % len(devs)
            dirty = True
        elif key in ("UP", "k") and devs:
            sel = (sel - 1) % len(devs)
            dirty = True
        elif key == "HOME" and devs:
            sel, dirty = 0, True
        elif key == "END" and devs:
            sel, dirty = len(devs) - 1, True
        elif key == "ENTER" and devs:
            return devs[sel]
        elif key and key.isdigit() and key != "0" and devs:
            i = int(key) - 1
            if i < len(devs):
                return devs[i]
        elif key == "b" and devs:
            d = devs[sel]
            nxt = next((x for x in BAUD_CYCLE if x > d.baud), BAUD_CYCLE[0])
            overrides[d.key] = nxt
            devs = enumerate_devices(cfg, overrides, cli_baud, show_all)
            sel = next((i for i, x in enumerate(devs) if x.key == d.key), sel)
            dirty = True
        elif key == "a" and devs:
            d = devs[sel]

            def draw(text, _d=devs, _s=sel):
                _render(_d, _s, fresh, None, " ", "", cfg_path, resumable, text)

            name = _text_prompt(draw, "name for this device: ", d.alias or "")
            if name is None:
                msg = "cancelled"
            else:
                problem = _valid_alias(name, cfg, current=d.alias)
                if problem:
                    msg = problem
                elif d.alias == name:
                    msg = "unchanged"
                elif d.alias:
                    msg = (f"renamed to [{name}]" if rename_alias(cfg_path, d.alias, name)
                           else f"could not find [{d.alias}] in {cfg_path}")
                else:
                    add_alias(cfg_path, name, d)
                    msg = f"saved as [{name}] in {cfg_path}"
                cfg, _ = load_config(cfg_path)
                devs = enumerate_devices(cfg, overrides, cli_baud, show_all)
                sel = next((i for i, x in enumerate(devs) if x.key == d.key), sel)
                sel = max(0, min(sel, len(devs) - 1)) if devs else 0
            dirty = True
        elif key == "H":
            msg = f"theme: {next_theme()}"
            dirty = True
        elif key == "r":
            cfg, _ = load_config(cfg_path)
            devs = enumerate_devices(cfg, overrides, cli_baud, show_all)
            known = {d.key for d in devs}
            sel = max(0, min(sel, len(devs) - 1)) if devs else 0
            msg = "reloaded"
            dirty = True


def _waiting_label(cfg, key: str) -> str:
    return _match_alias(cfg, key) or key


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

PREFIX = 0x14  # ctrl-t, same as tio

QUIT, REPICK, NEXT, LOST = "quit", "repick", "next", "lost"

HELP = """\
 ctrl-t ?   list commands          ctrl-t c   show configuration
 ctrl-t q   quit porter            ctrl-t L   show line states
 ctrl-t d   back to device picker  ctrl-t g   toggle DTR/RTS
 ctrl-t n   next device            ctrl-t b   send break
 ctrl-t l   clear screen           ctrl-t e   toggle local echo
 ctrl-t H   high-contrast mode     ctrl-t ctrl-t   send literal ctrl-t\
"""


def _open_port(dev: Device, attempts: int = 10, delay: float = 0.3):
    """Open the port, retrying while the driver settles.

    A device node appears in the enumeration before its driver has finished
    attaching, so an auto-reconnect that fires 400ms after replug routinely
    beats the driver to the port.  Retry rather than bouncing to the picker.
    """
    last = None
    for attempt in range(1, attempts + 1):
        try:
            ser = serial.Serial()
            ser.port = dev.port
            ser.baudrate = dev.baud
            ser.timeout = 0.05
            ser.write_timeout = 2
            if dev.dtr is not None:
                ser.dtr = dev.dtr
            if dev.rts is not None:
                ser.rts = dev.rts
            ser.open()
            return ser, None
        except Exception as exc:
            last = exc
            if attempt == 1:
                note(f"opening {dev.port} ...")
            elif attempt % 3 == 0:
                note(f"still opening {dev.port} ({attempt}/{attempts}): {exc}")
            _beat()
            time.sleep(delay)
            # Give up early if the device left again.
            if not any(_identity(pt) == dev.key for pt in _scan_ports()):
                break
    return None, last


class Session:
    """A live connection.

    It stays open while the picker is on screen, so escaping the picker
    resumes instead of reopening: reopening pulses DTR on most USB-serial
    chips, which would reset the board out from under you.  Device output
    that arrives while the picker is up is buffered and flushed on resume.
    """

    def __init__(self, dev: Device) -> None:
        self.dev = dev
        self.ser = None
        self.echo = False
        self._armed = False
        self._thread = None
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._paused = False
        self._backlog = bytearray()
        self._lock = threading.Lock()

    def open(self) -> str | None:
        """Connect.  Returns None on success, or an error to report."""
        ser, err = _open_port(self.dev)
        if ser is None:
            return str(err)
        self.ser = ser
        self._thread = threading.Thread(target=self._read_loop, daemon=True,
                                        name="serial-reader")
        self._thread.start()
        note(f"{self.dev.label} on {self.dev.port} @ {self.dev.baud}"
             " - ctrl-t ? for help")
        return None

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(Exception):
            self.ser.cancel_read()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                # Wedged in a native read.  Leaking one handle is strictly
                # safer than closing it out from under the thread.
                note("serial reader did not stop; leaking that handle", T.warn)

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            held, self._backlog = bytes(self._backlog), bytearray()
        note(f"back on {self.dev.label}"
             + (f" (+{len(held)} bytes buffered)" if held else ""))
        if held:
            w_bytes(held)

    def _read_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    data = self.ser.read(self.ser.in_waiting or 1)
                except Exception:
                    # A yanked handle surfaces as SerialException, OSError or
                    # AttributeError on a None handle -- all mean the same.
                    self._lost.set()
                    return
                if not data or self._stop.is_set():
                    continue
                with self._lock:
                    if self._paused:
                        self._backlog.extend(data)
                        if len(self._backlog) > BACKLOG_MAX:
                            del self._backlog[:-BACKLOG_MAX]
                        data = b""
                if data:
                    try:
                        w_bytes(data)
                    except Exception:
                        self._stop.set()
                        return
        finally:
            # The reader closes its own port.  Closing from the main thread
            # while a ReadFile is in flight lets Windows recycle the handle
            # value, and the orphan then reads the next session's port.
            with contextlib.suppress(Exception):
                self.ser.close()

    def run(self) -> str:
        """Pump the keyboard until something interesting happens."""
        reason = QUIT
        next_check = time.monotonic() + 1.0
        misses = 0
        try:
            while True:
                if self._lost.is_set():
                    reason = LOST
                    break

                # Backstop: Linux can return empty reads forever, and a
                # removed device does not always raise.
                now = time.monotonic()
                if now >= next_check:
                    next_check = now + 1.0
                    present = any(_identity(p) == self.dev.key
                                  for p in _scan_ports())
                    misses = 0 if present else misses + 1
                    if misses >= 2:
                        reason = LOST
                        break

                _beat()
                data = _read_raw(0.05)
                if not data:
                    continue

                outgoing = bytearray()
                for b in data:
                    if self._armed:
                        self._armed = False
                        cmd = _command(b, self.ser, self.dev, outgoing)
                        if cmd == "echo":
                            self.echo = not self.echo
                            note(f"local echo {'on' if self.echo else 'off'}")
                        elif cmd:
                            reason = cmd
                            raise _Done
                    elif b == PREFIX:
                        self._armed = True
                    else:
                        outgoing.append(b)

                if outgoing:
                    try:
                        self.ser.write(bytes(outgoing))
                    except Exception:
                        reason = LOST
                        break
                    if self.echo:
                        w_bytes(bytes(outgoing))
        except _Done:
            pass

        if reason == LOST:
            note(f"{self.dev.label} disconnected", T.warn)
        return reason


class _Done(Exception):
    """Break out of the nested byte loop."""


def _command(b: int, ser, dev: Device, outgoing: bytearray):
    """Handle one ctrl-t command byte.  Returns a session reason, or None."""
    ch = chr(b) if 0x20 <= b < 0x7F else ""

    if b == PREFIX:
        outgoing.append(PREFIX)
        return None
    if ch == "?":
        w("\r\n" + T.muted + HELP.replace("\n", "\r\n") + T.reset + "\r\n")
    elif ch == "q":
        return QUIT
    elif ch == "d":
        return REPICK
    elif ch == "n":
        return NEXT
    elif ch == "e":
        return "echo"
    elif ch == "l":
        w(CLEAR_SCREEN)
    elif ch == "b":
        with contextlib.suppress(OSError, serial.SerialException):
            ser.send_break(0.25)
        note("break sent")
    elif ch == "c":
        note(f"{dev.label}  {dev.port}  {dev.baud} 8N1  "
             f"id={dev.key}  dtr={ser.dtr} rts={ser.rts}")
    elif ch == "L":
        try:
            note(f"cts={ser.cts} dsr={ser.dsr} ri={ser.ri} cd={ser.cd}")
        except (OSError, serial.SerialException) as exc:
            note(f"line states unavailable: {exc}", T.err)
    elif ch == "H":
        note(f"theme: {next_theme()}")
    elif ch == "g":
        note("toggle which line?  d=DTR  r=RTS")
        pick = _read_raw(3.0)
        if pick[:1] == b"d":
            ser.dtr = not ser.dtr
            note(f"DTR {'high' if ser.dtr else 'low'}")
        elif pick[:1] == b"r":
            ser.rts = not ser.rts
            note(f"RTS {'high' if ser.rts else 'low'}")
        else:
            note("cancelled")
    else:
        # Unknown command: pass both bytes through untouched.
        outgoing.append(PREFIX)
        outgoing.append(b)
    return None


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def cmd_list(cfg, cli_baud) -> int:
    devs = enumerate_devices(cfg, {}, cli_baud, show_all=True)
    if not devs:
        print("no serial devices")
        return 0
    print(f"{'PORT':<16} {'ID':<40} {'BAUD':>7}  LABEL")
    for d in devs:
        tag = "" if (d.vid is not None or d.alias) else "   (hidden: no USB id)"
        print(f"{d.port:<16} {d.key:<40} {d.baud:>7}  {d.label}{tag}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="porter",
        description="Serial terminal with a live device picker.")
    ap.add_argument("-b", "--baud", type=int,
                    help="baud rate; overrides config")
    ap.add_argument("-c", "--config", help="path to config file")
    ap.add_argument("-a", "--all", action="store_true",
                    help="also show ports with no USB id (COM1, Bluetooth, ...)")
    ap.add_argument("--list", action="store_true",
                    help="list devices with their stable ids, then exit")
    ap.add_argument("--no-reconnect", action="store_true",
                    help="do not wait for a disconnected device to return")
    ap.add_argument("--debug", nargs="?", const="porter-debug.log", metavar="LOG",
                    help="log a full thread dump if the main loop ever stalls")
    args = ap.parse_args(argv)

    path = config_path(args.config)
    cfg, warning = load_config(path)
    if warning:
        print(f"porter: config problem: {warning}", file=sys.stderr)

    start_theme = (_global_get(cfg, "theme") or "default").strip()
    if start_theme not in THEMES:
        print(f"porter: unknown theme {start_theme!r}; using default",
              file=sys.stderr)
        start_theme = "default"

    if args.list:
        return cmd_list(cfg, args.baud)

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("porter: needs an interactive terminal", file=sys.stderr)
        return 1

    if not path.exists():
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(STARTER_CONFIG, encoding="utf-8")
            print(f"porter: wrote a starter config to {path}")

    if args.debug:
        _start_watchdog(args.debug)
        print(f"porter: watchdog logging to {args.debug}")

    global SCANNER
    SCANNER = PortScanner()
    SCANNER.start()
    SCANNER.ready.wait(timeout=3.0)

    overrides: dict = {}
    last_key = None
    waiting = None
    next_dev = None
    current = None          # the live Session, or None

    with RawTerm():
        try:
            set_theme(start_theme)

            while True:
                if next_dev is None:
                    cfg, _ = load_config(path)
                    if current is not None:
                        current.pause()
                    with alt_screen():
                        choice = picker(cfg, path, overrides, args.baud,
                                        last_key, waiting,
                                        resumable=current is not None,
                                        show_all=args.all)
                    waiting = None
                    if choice is None:
                        break
                    if choice is CANCEL:
                        current.resume()
                    else:
                        next_dev = choice

                if next_dev is not None:
                    if current is not None:
                        current.close()
                        current = None
                    dev, next_dev = next_dev, None
                    fresh = Session(dev)
                    err = fresh.open()
                    if err:
                        note(f"cannot open {dev.port}: {err}", T.err)
                        continue
                    current, last_key = fresh, dev.key

                reason = current.run()

                if reason == QUIT:
                    break
                if reason == NEXT:
                    devs = enumerate_devices(cfg, overrides, args.baud, args.all)
                    if len(devs) > 1:
                        i = next((n for n, d in enumerate(devs)
                                  if d.key == current.dev.key), -1)
                        next_dev = devs[(i + 1) % len(devs)]
                    else:
                        note("no other device", T.warn)
                elif reason == LOST:
                    gone = current.dev.key
                    current.close()
                    current = None
                    if not args.no_reconnect:
                        waiting = gone
        finally:
            if current is not None:
                current.close()
            w(CUR_SHOW + SGR0 + (_OSC_RESET if T.fg else "") + "\r\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
