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
import queue
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
SAVE_CUR = "\x1b7"
REST_CUR = "\x1b8"
WRAP_OFF = CSI + "?7l"
WRAP_ON = CSI + "?7h"

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


_TICK = [time.monotonic(), 0]   # last beat, and beats since the watchdog looked
SUSPEND = 4.0                   # a tick gap this long means we were not running
SPIN = 200                      # ticks per second no loop has a reason to reach


def _beat() -> None:
    """Tick the main loop: mark it alive, and recover from a suspend.

    A gap that dwarfs every loop's poll timeout means the process was not
    running -- a laptop that slept, or a terminal session that was detached --
    and the terminal we come back to is not necessarily the one we left.
    """
    now = time.monotonic()
    if now - _TICK[0] > SUSPEND:
        arm_console()
    _TICK[0] = now
    _TICK[1] += 1


def _start_watchdog(path: str, stall: float = 8.0) -> None:
    """Log what the main loop is doing, and dump every thread when it misbehaves.

    Two failures need catching and neither leaves anything on screen: the main
    thread blocked in a native call, and the main thread spinning on input it
    discards, which shows up as a hot fan rather than a hang.  The once-a-minute
    line is there so a lockup found hours later still has a before and after.
    """
    log = open(path, "a", buffering=1, encoding="utf-8")
    log.write(f"\n=== porter started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    faulthandler.enable(file=log, all_threads=True)

    def run():
        next_report = 0.0
        while True:
            time.sleep(1.0)
            now = time.monotonic()
            behind, ticks = now - _TICK[0], _TICK[1]
            _TICK[1] = 0
            scan = WATCHER.last_scan if WATCHER is not None else 0.0
            health = f"{ticks} ticks/s, port scan {scan * 1000:.0f}ms"

            trouble = None
            if behind > stall:
                trouble = f"main loop stalled {behind:.1f}s"
                _TICK[0] = now
            elif ticks > SPIN:
                trouble = f"main loop spinning: {health}"
            if trouble:
                log.write(f"\n=== {trouble} at {time.strftime('%H:%M:%S')} ===\n")
                faulthandler.dump_traceback(file=log, all_threads=True)
            elif now >= next_report:
                next_report = now + 60.0
                log.write(f"{time.strftime('%H:%M:%S')}  {health}\n")

    threading.Thread(target=run, daemon=True, name="watchdog").start()


def w(text: str) -> None:
    """Write UI text to the terminal.

    There is no lock here, and nothing to contend for: every write to the
    terminal happens on the main thread.  Device output reaches the screen as
    an event painted by the main loop, not by the thread that read it, which
    is what retired the output lock along with the reader's share of the SGR
    filter's state.
    """
    sys.stdout.write(text)
    sys.stdout.flush()


class _SGRStrip:
    """Drop SGR sequences from a stream of device bytes.

    A high-contrast theme is worthless if the device paints its own dim blue
    over the top of it.  Only colour and attribute sequences go; cursor motion
    and erases pass through, so a full-screen program on the far end still
    works -- it just arrives monochrome.

    A sequence split across two reads is held back until it completes, which
    is why this carries state -- state only the main thread ever touches.
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
    data = _SGR.feed(data) if T.strip_sgr else _SGR.flush() + data
    if not data:
        return
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()




# --------------------------------------------------------------------------
# The event bus
# --------------------------------------------------------------------------

KEY, RX, GONE, PORTS = "key", "rx", "gone", "ports"

TICK = 0.2          # how often a waiting loop wakes to check the clock
POLL = 0.4          # picker idle wake-up


class Bus:
    """The one place porter waits.

    Every source porter reacts to -- the keyboard, the device's output, the
    port list -- runs on its own thread and only ever *posts* here.  The main
    loop's only wait is a get(), so the wait-for graph is a star with all its
    edges pointing at this queue and no thread ever holding it.  No cycle is
    expressible, which is what makes the design deadlock-free rather than
    merely deadlock-free-so-far.

    The queue is unbounded, so a post never blocks and no producer can ever
    end up waiting on the consumer.  Keeping it unbounded is the invariant --
    a capacity here would put the producers back into the wait graph, and
    output is bounded by `Screen` instead, where the main thread owns it.
    """

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()

    def post(self, kind: str, payload=None) -> None:
        self._q.put((kind, payload))

    def get(self, timeout: float):
        """The next event, or (None, None) if none arrived in time."""
        try:
            return self._q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None, None


BUS = Bus()

BACKLOG_MAX = 256 * 1024


class Screen:
    """Who owns the terminal right now: the device, or porter.

    The main loop never stops draining the bus -- it parks device bytes here
    while porter has something on screen.  That is what lets the queue stay
    unbounded: held output is capped in one place, by the single thread that
    owns it, so there is no lock and no coordination.

    Holds nest, because a page can open over a session that is already held.
    """

    def __init__(self) -> None:
        self.held = 0
        self._buf = bytearray()

    def hold(self) -> None:
        self.held += 1

    def release(self) -> bytes:
        """Give the terminal back, and hand over what arrived meanwhile."""
        self.held = max(0, self.held - 1)
        if self.held:
            return b""
        buf, self._buf = bytes(self._buf), bytearray()
        return buf

    def feed(self, data: bytes) -> None:
        """Device output: straight to the terminal, or into the hold."""
        if self.held:
            self._buf.extend(data)
            if len(self._buf) > BACKLOG_MAX:
                del self._buf[:-BACKLOG_MAX]
        else:
            w_bytes(data)


SCREEN = Screen()


def _flush(data: bytes) -> None:
    if data:
        w_bytes(data)


TOAST_SECS = 1.5


class _Toast:
    """A one-line ack painted over the session instead of into it.

    An ack answers a key you just pressed -- read once, then worthless in a
    capture of the device's output.  It is painted at the cursor with autowrap
    off, so an over-long one is clipped at the right margin instead of
    wrapping and scrolling a fragment into the very scrollback this exists to
    keep clean, and it is taken back with a plain erase-to-end-of-line.

    That erase is only exact while the cursor has not moved since the paint,
    which is what holding the device buys.  Restoring the cursor uses the
    terminal's one save slot, so a full-screen program on the far end can lose
    a cursor it saved earlier.

    One slot, so repeated presses replace rather than stack.
    """

    def __init__(self) -> None:
        self._up = False
        self._until = 0.0

    def show(self, text: str, style: str | None = None) -> None:
        self.clear()
        self._up = True
        self._until = time.monotonic() + TOAST_SECS
        SCREEN.hold()
        w(WRAP_OFF + SAVE_CUR + (T.muted if style is None else style)
          + f"[porter] {text}" + T.reset + REST_CUR + WRAP_ON)

    def clear(self) -> None:
        """Take the ack back and let the held output through."""
        if not self._up:
            return
        self._up = False
        w(SAVE_CUR + CLEAR_EOL + REST_CUR)
        _flush(SCREEN.release())

    def tick(self, now: float) -> None:
        if self._up and now >= self._until:
            self.clear()


_TOAST = _Toast()


def toast(text: str, style: str | None = None) -> None:
    """Show a transient ack that never reaches the scrollback."""
    _TOAST.show(text, style)


def note(text: str, style: str | None = None) -> None:
    """Write a porter status line into the session.

    Notes are the messages that earn their place in a capture of the device's
    output: what porter did to the wire, and what the connection did.  A log
    with a gap in it is worth less than one that says why.  Raw mode, so
    newlines must be explicit.
    """
    _TOAST.clear()          # a real event outranks an ack still on screen
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

# Windows console input.
#
# Reading INPUT_RECORDs directly is what makes the poll non-blocking: we ask
# how many events are queued and read only that many.  Resize, focus, menu and
# mouse records are discarded here rather than handed to the key parser.

_KEY_EVENT = 0x0001

# Used when ENABLE_VIRTUAL_TERMINAL_INPUT could not be set: synthesise the VT
# sequence the parser already understands.
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

    # INPUT_RECORD is 20 bytes on every Windows ABI.  A mismatch would misparse
    # every keystroke, so say so instead of reading garbage.
    if ctypes.sizeof(_INPUT_RECORD) != 20:
        sys.exit(f"porter: unexpected INPUT_RECORD layout "
                 f"({ctypes.sizeof(_INPUT_RECORD)} bytes)")

    _K = ctypes.windll.kernel32
    # A HANDLE is pointer-sized; ctypes would otherwise truncate it to a
    # 32-bit int on 64-bit Windows.
    _K.GetStdHandle.restype = ctypes.c_void_p
    _K.GetStdHandle.argtypes = [ctypes.c_uint32]
    _K.GetConsoleMode.argtypes = [ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_uint32)]
    _K.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _K.GetNumberOfConsoleInputEvents.argtypes = [ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_uint32)]
    _K.ReadConsoleInputW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_uint32,
                                     ctypes.POINTER(ctypes.c_uint32)]
    # ctypes releases the GIL for the duration of a windll call, so blocking
    # here does not stop the other threads -- which is the whole point.
    _K.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _K.WaitForSingleObject.restype = ctypes.c_uint32

_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102


_WIN_HIN = None           # console input handle, refreshed by _win_handles
_WIN_HOUT = None
_ARM_GAP = 1.0            # do not re-arm the console faster than this
_last_arm = [float("-inf")]


def _win_handles():
    """Fetch the console handles, replacing any we already hold.

    A terminal that reconnects hands out new handles for the same console, and
    the old ones stop answering, so nothing may cache them across a failure.
    """
    global _WIN_HIN, _WIN_HOUT
    _WIN_HIN = _K.GetStdHandle(_STD_INPUT)
    _WIN_HOUT = _K.GetStdHandle(_STD_OUTPUT)
    return _WIN_HIN, _WIN_HOUT


def arm_console() -> None:
    """Put the console input side in raw mode, on a fresh handle.

    Raw mode is not something you set once.  A read that stops answering and a
    return from suspend both land here: Windows hands back a console with line
    input and echo switched on, which presents as a keyboard that has stopped
    working.  Idempotent, and rate-limited so a handle that never recovers
    cannot turn the poll loop into a stream of console API calls.

    POSIX needs none of this -- a termios raw mode survives a suspend.
    """
    if not WINDOWS:
        return
    now = time.monotonic()
    if now - _last_arm[0] < _ARM_GAP:
        return
    _last_arm[0] = now

    hin, hout = _win_handles()
    mode = ctypes.c_uint32()
    if not _K.GetConsoleMode(hin, ctypes.byref(mode)):
        return
    raw = mode.value & ~(_ENABLE_PROCESSED_INPUT | _ENABLE_LINE_INPUT
                         | _ENABLE_ECHO_INPUT)
    # Legacy conhost: fall back to raw without VT input, where the 0x00/0xe0
    # special-key prefixes come through as virtual key codes instead.
    if not _K.SetConsoleMode(hin, raw | _ENABLE_VIRTUAL_TERMINAL_INPUT):
        _K.SetConsoleMode(hin, raw)
    if _K.GetConsoleMode(hout, ctypes.byref(mode)):
        _K.SetConsoleMode(hout, mode.value
                          | _ENABLE_VIRTUAL_TERMINAL_PROCESSING)


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
            hin, hout = _win_handles()
            mode_in = ctypes.c_uint32()
            mode_out = ctypes.c_uint32()
            _K.GetConsoleMode(hin, ctypes.byref(mode_in))
            _K.GetConsoleMode(hout, ctypes.byref(mode_out))
            self._saved_in = mode_in.value
            self._saved_out = mode_out.value
            arm_console()
        else:
            self._fd = sys.stdin.fileno()
            self._saved_in = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        return self

    def __exit__(self, *exc) -> bool:
        if self._saved_in is None:
            return False
        if WINDOWS:
            _K.SetConsoleMode(_WIN_HIN, self._saved_in)
            _K.SetConsoleMode(_WIN_HOUT, self._saved_out)
        else:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_in)
        return False


def _win_read_console() -> bytes:
    """Drain queued key events.  Never blocks; returns b'' when idle."""
    pending = ctypes.c_uint32()
    if not _K.GetNumberOfConsoleInputEvents(_WIN_HIN, ctypes.byref(pending)):
        arm_console()          # stale handle: the next poll uses the new one
        return b""
    if pending.value == 0:
        return b""

    count = min(pending.value, 128)
    recs = (_INPUT_RECORD * count)()
    got = ctypes.c_uint32()
    if not _K.ReadConsoleInputW(_WIN_HIN, recs, count, ctypes.byref(got)):
        arm_console()
        return b""

    out = bytearray()
    for i in range(got.value):
        rec = recs[i]
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


DEAD_WAIT = 0.25    # floor under a wait that failed instead of waiting


def _wait_input(timeout: float) -> bool:
    """Block until the keyboard has something, or `timeout` runs out.

    This is the wait that replaced a 250Hz poll, and it keeps one rule: a
    *failed* wait must still cost time.  A console handle that has gone --
    which is what a sleep/wake cycle can hand back on Windows -- fails
    immediately, and a wait that returns instantly for ever is the same pegged
    core the poll was, only written more elegantly.  Going event-driven does
    not save you from a dead handle; this is what does.
    """
    if not WINDOWS:
        try:
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
        except (OSError, ValueError):
            time.sleep(min(timeout, DEAD_WAIT))
            return False
        return bool(ready)

    rc = _K.WaitForSingleObject(_WIN_HIN, int(max(0.0, timeout) * 1000))
    if rc == _WAIT_OBJECT_0:
        return True
    if rc == _WAIT_TIMEOUT:
        return False
    arm_console()               # stale handle: get a fresh one for next time
    time.sleep(min(timeout, DEAD_WAIT))
    return False


def _read_raw(timeout: float) -> bytes:
    """Keyboard bytes, or b'' after `timeout`.  Blocks; never spins.

    A signalled console may hold nothing but a resize, focus or mouse record,
    so this can legitimately return b'' before the timeout.  That is not a
    spin: reading consumed those records, which leaves the handle unsignalled,
    so the next wait blocks properly.  Consuming what it wakes for is the
    property that retired the old drain pacing.
    """
    if not _wait_input(timeout):
        return b""
    if WINDOWS:
        return _win_read_console()
    try:
        return os.read(sys.stdin.fileno(), 1024)
    except OSError:
        return b""


_ARROWS = {0x41: "UP", 0x42: "DOWN", 0x43: "RIGHT", 0x44: "LEFT",
           0x48: "HOME", 0x46: "END"}
_pending = bytearray()


ESC_WAIT = 0.03     # how long the rest of an escape sequence has to arrive
ESC_TRIES = 2       # ... and how many times we are willing to wait for it


def _tail_partial(data: bytes) -> bool:
    """True if `data` stops part-way through an escape sequence."""
    i = data.rfind(0x1B)
    if i < 0:
        return False
    tail = data[i:]
    if len(tail) == 1:
        return True                         # bare ESC, or the start of one
    if tail[1] not in (0x5B, 0x4F):         # not CSI/SS3: it was ESC + a key
        return False
    return not any(0x40 <= b <= 0x7E for b in tail[2:])


def _read_chunk(timeout: float) -> bytes:
    """Keyboard bytes with any escape sequence at the tail completed.

    Sequences are kept whole here, once, so that neither consumer has to time
    the byte stream: the session forwards a chunk to the device untouched, and
    the picker can read a trailing escape as the ESC key rather than half of
    an arrow.  It is also what lets the parser below be a pure function --
    the old split-across-reads bookkeeping, and the phantom keystrokes it
    produced when it guessed wrong, have nowhere left to live.
    """
    data = _read_raw(timeout)
    for _ in range(ESC_TRIES):
        if not data or not _tail_partial(data):
            break
        data += _read_raw(ESC_WAIT)
    return data


def _sequence_end():
    """Index of the final byte of the CSI/SS3 sequence at the head of
    `_pending`, or None when there is not a whole one there yet."""
    if len(_pending) < 3 or _pending[1] not in (0x5B, 0x4F):
        return None
    i = 2
    while i < len(_pending) and not 0x40 <= _pending[i] <= 0x7E:
        i += 1
    return None if i >= len(_pending) else i


def _next_key():
    """Pop one key token off `_pending`, or None if what came off was not a key.

    Pure: it never reads.  `_read_chunk` guarantees whole sequences, so an
    escape with nothing parseable after it really was the ESC key.
    """
    if not _pending:
        return None

    if _pending[0] != 0x1B:
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

    end = _sequence_end()
    if end is None:
        del _pending[0]
        return "ESC"
    final = _pending[end]
    del _pending[: end + 1]
    return _ARROWS.get(final)               # anything else is a report, not a key


def keys(data: bytes) -> list:
    """The key tokens in `data`, with reports and other non-keys dropped.

    Terminal reports -- focus in and out, cursor position, mouse -- share the
    channel with keystrokes and are not keys.  Discarding them is free now:
    the read that consumed them left the handle unsignalled, so nothing comes
    straight back round for more.
    """
    _pending.extend(data)
    out = []
    while _pending:
        key = _next_key()
        if key is not None:
            out.append(key)
    return out


class KeyReader(threading.Thread):
    """Turn keystrokes into KEY events carrying raw bytes.

    Bytes, not tokens, because the session is a transparent pipe: an arrow key
    has to reach the far-side REPL as the three bytes it came in as.  Only the
    picker asks for tokens, and it asks `keys()` for them.

    The keyboard is the one source that earns a native blocking wait -- it is
    where the old poll lived, and every platform makes the wait cheap.
    """

    def __init__(self, bus: Bus) -> None:
        super().__init__(daemon=True, name="key-reader")
        self.bus = bus
        self._quit = threading.Event()      # not _stop: see PortWatcher

    def stop(self) -> None:
        self._quit.set()

    def run(self) -> None:
        while not self._quit.is_set():
            data = _read_chunk(TICK)
            if data:
                self.bus.post(KEY, data)


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
# Start in a high-contrast palette instead of the default colours -- `h`
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


FAST_SCAN = 0.4     # the picker is on screen: this list is what is being read
IDLE_SCAN = 1.0     # a session is live: nobody is reading the list
WATCHER = None


class PortWatcher(threading.Thread):
    """Post an event whenever the set of serial ports changes.

    A timer rather than an OS notification, deliberately.  netlink and
    WM_DEVICECHANGE only say that *something* changed -- finding out what
    still means calling comports() -- so the expensive part is identical and
    all native notification buys is a lower scan rate.  This is one
    implementation instead of three plus a hole where macOS goes, and if the
    latency ever does matter, a native source drops in as one more producer on
    the same bus with nothing else moving.

    What makes it event-driven is the diff: the tick is an implementation
    detail of this thread, and nothing downstream polls a snapshot.  Devices
    appear and disappear in the picker because this said so.

    comports() is a pure-Python ctypes walk of the device tree, so it holds
    the GIL for most of its duration and its cost is set by the state of the
    machine rather than by anything porter controls.  Hence the share guard.
    """

    SHARE = 8           # sleep at least this many times the last scan's cost

    def __init__(self, bus: Bus) -> None:
        super().__init__(daemon=True, name="port-watcher")
        self.bus = bus
        self.interval = FAST_SCAN
        self.last_scan = 0.0        # seconds the most recent scan took
        self.ready = threading.Event()
        self._ports: list = []
        # Not _stop: threading.Thread._stop is a method join() calls, and an
        # attribute of that name shadows it and breaks join() outright.
        self._quit = threading.Event()

    def attention(self, wanted: bool) -> None:
        """Scan fast while the device list is on screen, slowly when it is not.

        In a live session the list is unread, and the one device that matters
        is watched far more closely than any scan could manage: its reader
        faults the instant the cable goes.
        """
        self.interval = FAST_SCAN if wanted else IDLE_SCAN

    def stop(self) -> None:
        self._quit.set()

    def ports(self) -> list:
        # Rebound whole, never mutated in place, so this needs no lock.
        return list(self._ports)

    def run(self) -> None:
        seen = None
        while not self._quit.is_set():
            started = time.monotonic()
            try:
                ports = list(list_ports.comports())
            except Exception:
                # Enumeration failing means "unknown", not "all unplugged":
                # keep the last snapshot rather than report every device gone.
                ports = None
            self.last_scan = time.monotonic() - started

            if ports is not None:
                self._ports = ports
                self.ready.set()
                now_keys = {_identity(p) for p in ports}
                if now_keys != seen:
                    seen = now_keys
                    self.bus.post(PORTS, ports)

            # Proportional, and it must stay that way.  An absolute ceiling
            # here reads like a bound on how stale the list can get, but
            # because the walk holds the GIL it really puts a *floor* under
            # this thread's duty cycle as scans get slower: a 10s enumeration
            # under a 2s cap is 83% of a core with the GIL held, the main
            # thread stops being scheduled, and porter locks up with a dead
            # keyboard.  A stale list is a far smaller problem than that.
            self._quit.wait(max(self.interval, self.last_scan * self.SHARE))


def _scan_ports() -> list:
    if WATCHER is not None:
        return WATCHER.ports()
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

class _Cancel:
    """Sentinel: leave the picker without disturbing the live session."""


CANCEL = _Cancel()


def _text_prompt(draw, label: str, initial: str = "") -> str | None:
    """Modal one-line input drawn under the device list.  None if cancelled."""
    buf = list(initial)
    dirty = True
    while True:
        if dirty:
            draw(label + "".join(buf) + "_")
            dirty = False
        _beat()
        kind, payload = BUS.get(POLL)
        if kind == RX:
            SCREEN.feed(payload)            # the picker owns the screen
            continue
        if kind != KEY:
            continue
        for key in keys(payload):
            if key == "ENTER":
                return "".join(buf).strip()
            if key in ("ESC", "CTRL-C"):
                return None
            if key == "BACK":
                if buf:
                    buf.pop()
            elif len(key) == 1 and key.isprintable():
                buf.append(key)
            dirty = True


def _render(devs, sel, fresh, msg, cfg_path,
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
    if msg:
        rows.append(f" {T.ok}{msg}{T.reset}")
    rows.append("")

    if prompt is not None:
        rows.append(f" {T.bold}{prompt}{T.reset}")
        rows.append(T.muted + " enter save  .  esc cancel" + T.reset)
    else:
        rows.append(T.muted + " j/k or arrows select  .  enter connect"
                    "  .  1-9 jump  .  b baud  .  a name" + T.reset)
        foot = " h high contrast"
        foot += "  .  esc resume  .  q quit" if resumable else "  .  q quit"
        rows.append(T.muted + foot + T.reset)
        rows.append(T.muted + f" aliases: {cfg_path}" + T.reset)

    w(T.reset + HOME + (CLEAR_EOL + "\r\n").join(rows) + CLEAR_EOL + CLEAR_EOS)


def picker(cfg, cfg_path: Path, overrides: dict, cli_baud: int | None,
           preselect: str | None, session=None,
           resumable: bool = False, show_all: bool = False):
    """Live device list, driven by events.

    Nothing in here polls.  The loop waits on the bus, and devices appear and
    disappear because the watcher said the port set changed -- the tick that
    noticed lives in that thread and is not this loop's business.

    Losing a device always lands here, and that is not cosmetic.  An earlier
    version waited in place watching only the device it had lost, which made
    anything *else* plugged in during the wait invisible and left porter
    looking wedged with no way out but a keystroke.  The picker is already a
    live device monitor; putting the wait anywhere else re-creates that dead
    end.

    Returns a Device to connect to, CANCEL to go back to the live session, or
    None to quit.
    """
    devs = enumerate_devices(cfg, overrides, cli_baud, show_all)
    known = {d.key for d in devs}
    fresh: set[str] = set()
    msg = ""
    dirty = True

    sel = 0
    if preselect:
        for i, d in enumerate(devs):
            if d.key == preselect:
                sel = i
                break

    while True:
        if dirty:
            _render(devs, sel, fresh, msg, cfg_path, resumable)
            dirty = False

        _beat()
        kind, payload = BUS.get(POLL)

        if kind == RX:
            SCREEN.feed(payload)        # held: the picker owns the screen
            continue

        if kind == GONE:
            # The session died while its own scrollback was off screen.  Mark
            # it and leave esc alone: resuming a session that is already lost
            # reports the disconnect and lands straight back here, whereas
            # clearing `resumable` would silently turn esc into quit.
            if session is not None and payload == session.dev.key:
                session.lost = True
            continue

        if kind == PORTS:
            new_devs = enumerate_devices(cfg, overrides, cli_baud, show_all)
            new_keys = {d.key for d in new_devs}
            if new_keys == known:
                continue
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
            continue

        if kind != KEY:
            continue

        for key in keys(payload):
            if fresh or msg:            # the +new tag has served its purpose
                fresh, msg = set(), ""
                dirty = True

            if key in ("q", "CTRL-C"):
                return None
            if key == "ESC":
                # Escape means "never mind" when there is a session to go back
                # to, and only means quit when there is nothing behind it.
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
                    _render(_d, _s, fresh, "", cfg_path, resumable, text)

                name = _text_prompt(draw, "name for this device: ",
                                    d.alias or "")
                if name is None:
                    msg = "cancelled"
                else:
                    problem = _valid_alias(name, cfg, current=d.alias)
                    if problem:
                        msg = problem
                    elif d.alias == name:
                        msg = "unchanged"
                    elif d.alias:
                        msg = (f"renamed to [{name}]"
                               if rename_alias(cfg_path, d.alias, name)
                               else f"could not find [{d.alias}] in {cfg_path}")
                    else:
                        add_alias(cfg_path, name, d)
                        msg = f"saved as [{name}] in {cfg_path}"
                    cfg, _ = load_config(cfg_path)
                    devs = enumerate_devices(cfg, overrides, cli_baud, show_all)
                    sel = next((i for i, x in enumerate(devs)
                                if x.key == d.key), sel)
                    sel = max(0, min(sel, len(devs) - 1)) if devs else 0
                dirty = True
            elif key == "h":
                msg = f"theme: {next_theme()}"
                dirty = True
            elif key == "r":
                cfg, _ = load_config(cfg_path)
                devs = enumerate_devices(cfg, overrides, cli_baud, show_all)
                known = {d.key for d in devs}
                sel = max(0, min(sel, len(devs) - 1)) if devs else 0
                msg = "reloaded"
                dirty = True


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

PREFIX = 0x14  # ctrl-t, same as tio

QUIT, REPICK, NEXT, LOST = "quit", "repick", "next", "lost"

GRACE = 1.0       # how long a device may be missing from a scan before we act

HELP = """\
 ctrl-t ?   list commands          ctrl-t c   show configuration
 ctrl-t q   quit porter            ctrl-t L   show line states
 ctrl-t d   back to device picker  ctrl-t g   toggle DTR/RTS
 ctrl-t n   next device            ctrl-t b   send break
 ctrl-t l   clear screen           ctrl-t e   toggle local echo
 ctrl-t h   high-contrast mode     ctrl-t ctrl-t   send literal ctrl-t\
"""


def _open_port(dev: Device, attempts: int = 10, delay: float = 0.3):
    """Open the port, retrying while the driver settles.

    A device node appears in the enumeration before its driver has finished
    attaching, so selecting a device the moment it shows up in the picker
    routinely beats the driver to the port.  Retry rather than reporting a
    failure the user can only fix by pressing enter again.
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
    chips, which would reset the board out from under you.  Device output that
    arrives while porter owns the screen is held by `Screen` and flushed on
    the way back.

    No locks.  The reader thread only posts to the bus, and everything that
    touches the terminal or the held bytes happens on the main thread.
    """

    def __init__(self, dev: Device, bus: Bus) -> None:
        self.dev = dev
        self.bus = bus
        self.ser = None
        self.echo = False
        self.lost = False       # set by whoever sees GONE first
        self._armed = False
        self._thread = None
        self._stop = threading.Event()

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
                # safer than closing it out from under the thread -- and a
                # thread leaked *blocked* holds no lock and no GIL, which is
                # the whole reason the empty-read wait below matters.
                note("serial reader did not stop; leaking that handle", T.warn)

    def resume(self) -> None:
        """Come back from the picker, naming the device we returned to.

        Silent when the device went while the picker was up: run() is about to
        report the disconnect, and "back on X" immediately above "X
        disconnected" reads like porter lost track of what happened.
        """
        held = SCREEN.release()
        if not self.lost:
            note(f"back on {self.dev.label}"
                 + (f" (+{len(held)} bytes buffered)" if held else ""))
        _flush(held)

    def _read_loop(self) -> None:
        """Read the device and post it.  Never touches the terminal."""
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    data = self.ser.read(self.ser.in_waiting or 1)
                except Exception:
                    # A yanked handle surfaces as SerialException, OSError or
                    # AttributeError on a None handle -- all mean the same.
                    self.bus.post(GONE, self.dev.key)
                    return
                if data:
                    self.bus.post(RX, data)
                    continue
                # An empty read must still cost its timeout.  A handle whose
                # device has gone can return empty the instant it is called,
                # and then this loop is a spin that holds the GIL -- and a
                # reader that will not stop is leaked rather than joined, so
                # one dead port could starve the main thread for the rest of
                # the run.  Wait out the remainder either way.
                idle = self.ser.timeout - (time.monotonic() - started)
                if idle > 0:
                    self._stop.wait(idle)
        finally:
            # The reader closes its own port.  Closing from the main thread
            # while a ReadFile is in flight lets Windows recycle the handle
            # value, and the orphan then reads the next session's port.
            with contextlib.suppress(Exception):
                self.ser.close()

    def run(self) -> str:
        """Wait on the bus until something takes us out of the session."""
        if self.lost:
            note(f"{self.dev.label} disconnected", T.warn)
            return LOST

        reason = QUIT
        absent_since = None
        try:
            while True:
                kind, payload = BUS.get(TICK)
                now = time.monotonic()
                _beat()
                _TOAST.tick(now)

                # Checked every time round, not only on a timeout: a device
                # that vanished from the port list while still producing
                # output would otherwise never come up for judgement.
                if absent_since is not None and now - absent_since >= GRACE:
                    reason = LOST
                    break

                if kind == RX:
                    SCREEN.feed(payload)
                    continue
                if kind == GONE:
                    self.lost = True
                    reason = LOST
                    break
                if kind == PORTS:
                    # Backstop for a device that stops answering without ever
                    # faulting: Linux can return empty reads for ever.  The
                    # snapshot is edge-triggered, so absence is news -- but
                    # give it a moment, because a driver reshuffling can drop
                    # a device from one enumeration and put it back.
                    present = any(_identity(p) == self.dev.key for p in payload)
                    absent_since = None if present else (absent_since or now)
                    continue
                if kind is None:
                    continue

                # A key chunk.  Bytes, so an arrow reaches the far side whole.
                # The ack has to come down before the keystroke is echoed, or
                # the erase would take back the echo instead.
                _TOAST.clear()
                outgoing = bytearray()
                for b in payload:
                    if self._armed:
                        self._armed = False
                        cmd = _command(b, self, outgoing)
                        if cmd == "echo":
                            self.echo = not self.echo
                            toast(f"local echo "
                                  f"{'on' if self.echo else 'off'}")
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
                        SCREEN.feed(bytes(outgoing))
        except _Done:
            pass

        if reason == LOST:
            note(f"{self.dev.label} disconnected", T.warn)
        return reason


class _Done(Exception):
    """Break out of the nested byte loop."""


# A page is modal, so it holds the device off the screen for as long as it is
# up.  Backstop the wait: walking away from an open help page would otherwise
# hold output until BACKLOG_MAX and start dropping the oldest of it.
PAGE_SECS = 60.0


def _page(body: str, hint: str = "any key to resume",
          timeout: float = PAGE_SECS) -> bytes:
    """Answer a question about porter on a page the scrollback never sees.

    Modal, like the picker: keys typed while it is up belong to the page and
    do not reach the device.  Output is held rather than dropped and flushed
    into the main buffer on the way out, so the page costs the log nothing.
    """
    _TOAST.clear()
    SCREEN.hold()
    try:
        with alt_screen():
            w(HOME + body.replace("\n", "\r\n")
              + "\r\n\r\n" + T.muted + " " + hint + T.reset)
            deadline = time.monotonic() + timeout
            while True:
                _beat()
                left = deadline - time.monotonic()
                if left <= 0:
                    return b""
                kind, payload = BUS.get(min(TICK, left))
                if kind == RX:
                    SCREEN.feed(payload)
                elif kind == KEY:
                    return payload
                elif kind == GONE:
                    BUS.post(GONE, payload)     # not this loop's to consume
                    return b""
    finally:
        _flush(SCREEN.release())


def _command(b: int, sess, outgoing: bytearray):
    """Handle one ctrl-t command byte.  Returns a session reason, or None.

    Where the answer goes is the rule: what porter did to the wire or to the
    connection is a note, in the scrollback; what porter has to say about
    itself is a page or a toast, and leaves no trace in the capture.
    """
    ch = chr(b) if 0x20 <= b < 0x7F else ""
    ser, dev = sess.ser, sess.dev

    if b == PREFIX:
        outgoing.append(PREFIX)
        return None
    if ch == "?":
        _page(T.muted + HELP + T.reset)
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
        rows = [("device", dev.label), ("port", dev.port),
                ("baud", f"{dev.baud} 8N1"), ("id", dev.key),
                ("lines", f"dtr={ser.dtr} rts={ser.rts}"),
                ("theme", T.name), ("echo", "on" if sess.echo else "off")]
        _page("".join(f" {T.muted}{k:<8}{T.reset}{v}\n" for k, v in rows))
    elif ch == "L":
        # A reading off the wire, not a fact about porter: it belongs in the log.
        try:
            note(f"cts={ser.cts} dsr={ser.dsr} ri={ser.ri} cd={ser.cd}")
        except (OSError, serial.SerialException) as exc:
            note(f"line states unavailable: {exc}", T.err)
    elif ch == "h":
        toast(f"theme: {next_theme()}")
    elif ch == "g":
        pick = _page(f" {T.bold}toggle which line?{T.reset}\n\n"
                     f" {T.bold}d{T.reset}   DTR is currently "
                     f"{'high' if ser.dtr else 'low'}\n"
                     f" {T.bold}r{T.reset}   RTS is currently "
                     f"{'high' if ser.rts else 'low'}",
                     "any other key cancels", 10.0)
        if pick[:1] == b"d":
            ser.dtr = not ser.dtr
            note(f"DTR {'high' if ser.dtr else 'low'}")
        elif pick[:1] == b"r":
            ser.rts = not ser.rts
            note(f"RTS {'high' if ser.rts else 'low'}")
        else:
            toast("cancelled")          # nothing happened, so log nothing
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

    global WATCHER
    WATCHER = PortWatcher(BUS)
    WATCHER.start()
    WATCHER.ready.wait(timeout=3.0)

    overrides: dict = {}
    last_key = None
    next_dev = None
    current = None          # the live Session, or None

    with RawTerm():
        reader = KeyReader(BUS)
        reader.start()
        try:
            set_theme(start_theme)

            while True:
                if next_dev is None:
                    cfg, _ = load_config(path)
                    _TOAST.clear()
                    SCREEN.hold()       # the picker owns the screen now
                    WATCHER.attention(True)
                    try:
                        with alt_screen():
                            choice = picker(
                                cfg, path, overrides, args.baud, last_key,
                                current,
                                resumable=(current is not None
                                           and not current.lost),
                                show_all=args.all)
                    finally:
                        WATCHER.attention(False)

                    if choice is CANCEL:
                        current.resume()        # releases the hold, and says so
                    else:
                        SCREEN.release()        # switching or quitting: drop it
                        if choice is None:
                            break
                        next_dev = choice

                if next_dev is not None:
                    if current is not None:
                        current.close()
                        current = None
                    dev, next_dev = next_dev, None
                    fresh = Session(dev, BUS)
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
                    # Always back to the picker: whatever else is plugged in
                    # stays visible, so losing one device is never a dead end.
                    current.close()
                    current = None
        finally:
            # Join before RawTerm restores the console: a reader still inside
            # a wait can call arm_console() on a failure, and doing that after
            # the restore would hand the terminal back in raw mode.
            reader.stop()
            reader.join(timeout=1.0)
            if current is not None:
                current.close()
            w(CUR_SHOW + SGR0 + (_OSC_RESET if T.fg else "") + "\r\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
