"""Unit tests for porter's pure logic -- no tty, no hardware."""
import sys, types, tempfile, pathlib, configparser, contextlib, io, time, threading
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import porter

fails = []
def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r} want {want!r}")
    else:
        print(f"  ok  {name}")

class P:  # stand-in for ListPortInfo
    def __init__(self, device, vid=None, pid=None, serial_number=None,
                 location=None, description=None):
        self.device, self.vid, self.pid = device, vid, pid
        self.serial_number, self.location = serial_number, location
        self.description = description or device

# BaseException, not Exception: the watcher deliberately swallows any
# Exception out of comports(), so a sentinel deriving from Exception
# would be caught there and the run loop would never end.
class _Stop(BaseException): pass
def _stop(): raise _Stop

@contextlib.contextmanager
def mock(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)

class _Clock:
    """monotonic() that advances by `cost` across every comports() call."""
    def __init__(self, cost): self.t, self.cost = 1000.0, cost
    def monotonic(self): return self.t

print("identity")
check("full", porter._identity(P("COM14", 0x239a, 0x80f4, "DF62AA")), "239a:80f4:DF62AA")
check("no-serial-has-location", porter._identity(P("COM7", 0x0403, 0x6001, None, "1-4.2")), "0403:6001@1-4.2")
check("no-serial-no-location", porter._identity(P("COM3", 0x10c4, 0xea60)), "10c4:ea60:COM3")
check("not-usb", porter._identity(P("/dev/ttyS0")), "/dev/ttyS0")

print("alias matching")
cfgtext = """
[porter]
baudrate = 9600
exclude = COM1, /dev/ttyS*

[codebot-3]
id = 239a:80f4:DF62AA
baudrate = 115200

[any-ftdi]
id = 0403:6001
baudrate = 921600
dtr = true
"""
tmp = pathlib.Path(tempfile.mkdtemp()) / "config"
tmp.write_text(cfgtext)
cfg, warn = porter.load_config(tmp)
check("no warning", warn, None)
check("exact wins", porter._match_alias(cfg, "239a:80f4:DF62AA"), "codebot-3")
check("prefix vid:pid", porter._match_alias(cfg, "0403:6001:ABC123"), "any-ftdi")
check("prefix via location", porter._match_alias(cfg, "0403:6001@1-4.2"), "any-ftdi")
check("no match", porter._match_alias(cfg, "1234:5678:XX"), None)
check("prefix must be delimited", porter._match_alias(cfg, "0403:60019:X"), None)

print("exclude")
check("excl exact", porter._excluded(cfg, "COM1", "x", "y"), True)
check("excl glob", porter._excluded(cfg, "/dev/ttyS0", "x", "y"), True)
check("keep", porter._excluded(cfg, "COM14", "x", "y"), False)

print("enumerate + baud precedence + sort")
ports = [P("/dev/ttyS0"),
         P("COM3", 0x10c4, 0xea60, "AAA", None, "USB Serial"),
         P("COM14", 0x239a, 0x80f4, "DF62AA", None, "CircuitPython"),
         P("COM7", 0x0403, 0x6001, "ZZZ", None, "FTDI")]
porter.list_ports.comports = lambda: ports

d = porter.enumerate_devices(cfg, {}, None)
by = {x.key: x for x in d}
CB, FT, US = "239a:80f4:DF62AA", "0403:6001:ZZZ", "10c4:ea60:AAA"
check("ttyS0 excluded", "/dev/ttyS0" in [x.port for x in d], False)
check("aliased first, then alpha", [x.label for x in d], ["any-ftdi", "codebot-3", "USB Serial"])
check("alias baud", by[CB].baud, 115200)
check("prefix alias baud", by[FT].baud, 921600)
check("global baud", by[US].baud, 9600)
check("dtr parsed", by[FT].dtr, True)
check("dtr unset", by[CB].dtr, None)
check("label from alias", by[CB].label, "codebot-3")
check("label fallback", by[US].label, "USB Serial")
check("ident render", by[US].ident, "10c4:ea60")

d = porter.enumerate_devices(cfg, {}, 57600)
check("cli baud beats alias", sorted(x.baud for x in d), [57600, 57600, 57600])
d = porter.enumerate_devices(cfg, {CB: 230400}, 57600)
check("override beats cli", {x.key: x.baud for x in d}[CB], 230400)
check("override is per-device", {x.key: x.baud for x in d}[FT], 57600)

print("stable order under renumbering")
ports[2] = P("COM22", 0x239a, 0x80f4, "DF62AA", None, "CircuitPython")
d2 = porter.enumerate_devices(cfg, {}, None)
check("order survives renumber", [x.label for x in d2], ["any-ftdi", "codebot-3", "USB Serial"])
check("identity survives renumber", {x.key for x in d2} == {CB, FT, US}, True)
check("port did change", {x.key: x.port for x in d2}[CB], "COM22")

print("non-USB ports hidden")
ports.append(P("COM1", None, None, None, None, "HHD Software Bridged Serial"))
ports.append(P("COM8", None, None, None, None, "Standard Serial over Bluetooth"))
cfg_ns = configparser.ConfigParser()
cfg_ns.read_string("[porter]\nbaudrate = 115200\n")
d3 = porter.enumerate_devices(cfg_ns, {}, None)
check("no-vidpid hidden by default", [x.port for x in d3].count("COM1"), 0)
check("bluetooth hidden", [x.port for x in d3].count("COM8"), 0)
check("usb ports still shown", len(d3), 3)
d4 = porter.enumerate_devices(cfg_ns, {}, None, show_all=True)
check("--all reveals them", [x.port for x in d4].count("COM1"), 1)
cfg_ns.read_string("[porter]\nshow_all = true\n")
check("show_all config reveals them",
      [x.port for x in porter.enumerate_devices(cfg_ns, {}, None)].count("COM1"), 1)
cfg_al = configparser.ConfigParser()
cfg_al.read_string("[bench-rs232]\nid = COM1\n")
d5 = porter.enumerate_devices(cfg_al, {}, None)
check("aliased non-USB port survives", [x.label for x in d5].count("bench-rs232"), 1)
ports[:] = ports[:4]

print("alias naming")
dev = [x for x in d2 if x.key == US][0]
check("empty rejected", porter._valid_alias("", cfg), "name cannot be empty")
check("bracket rejected", porter._valid_alias("a]b", cfg),
      "name cannot contain [ ] or newlines")
check("reserved rejected", porter._valid_alias("porter", cfg), "'porter' is reserved")
check("dupe rejected", porter._valid_alias("codebot-3", cfg), "'codebot-3' is already used")
check("rename to itself allowed", porter._valid_alias("codebot-3", cfg, current="codebot-3"), None)
check("spaces allowed", porter._valid_alias("CodeBot #3", cfg), None)

before = tmp.read_text()
porter.add_alias(tmp, "CodeBot #3", dev)
cfg2, _ = porter.load_config(tmp)
check("alias saved under chosen name", porter._match_alias(cfg2, dev.key), "CodeBot #3")
check("comments preserved on add", before in tmp.read_text(), True)

check("rename works", porter.rename_alias(tmp, "CodeBot #3", "CodeBot 3"), True)
cfg3, _ = porter.load_config(tmp)
check("renamed section matches device", porter._match_alias(cfg3, dev.key), "CodeBot 3")
check("id survives rename", cfg3["CodeBot 3"]["id"], dev.key)
check("comments preserved on rename", before in tmp.read_text(), True)
check("rename of missing section", porter.rename_alias(tmp, "nope", "x"), False)

print("themes")
with contextlib.redirect_stdout(io.StringIO()):   # swallow the OSC it emits
    cycle = [porter.next_theme() for _ in range(3)]
check("cycle wraps back to default", cycle,
      ["contrast-dark", "contrast-light", "default"])
check("high contrast spends no colour on roles",
      porter.THEMES["contrast-dark"].alias, porter.BOLD)
check("reset re-establishes the theme's own colours",
      porter.THEMES["contrast-light"].reset, porter.SGR0 + porter.CSI + "0;30;107m")
check("default theme leaves the terminal's colours alone",
      porter.THEMES["default"].reset, porter.SGR0)

print("device colours flattened under a high-contrast theme")
def strip(*chunks):
    f = porter._SGRStrip()
    return b"".join(f.feed(c) for c in chunks) + f.flush()

check("colour dropped", strip(b"a\x1b[31mred\x1b[0mb"), b"aredb")
check("256-colour dropped", strip(b"\x1b[1;38;5;208mX"), b"X")
check("cursor motion survives", strip(b"\x1b[2J\x1b[H\x1b[1;5Hx"),
      b"\x1b[2J\x1b[H\x1b[1;5Hx")
check("non-CSI escape survives", strip(b"\x1b(Bz"), b"\x1b(Bz")
check("sequence split across reads", strip(b"one\x1b[3", b"1mtwo"), b"onetwo")
check("split right after esc", strip(b"x\x1b", b"[32mgo"), b"xgo")
check("split cursor sequence survives", strip(b"\x1b[1", b";2H!"), b"\x1b[1;2H!")
check("a lone esc is not swallowed for ever",
      strip(b"\x1b[" + b"9" * 40), b"\x1b[" + b"9" * 40)
binary = bytes(range(256)).replace(b"\x1b", b"\x00")
check("binary passes through untouched", strip(binary), binary)

print("key parsing")

def toks(*chunks):
    """Feed whole chunks through the parser, as _read_chunk delivers them."""
    porter._pending.clear()
    out = []
    for c in chunks:
        out += porter.keys(c)
    return out

check("plain characters", toks(b"abc"), ["a", "b", "c"])
check("enter and tab", toks(b"\r\t"), ["ENTER", "TAB"])
check("control byte", toks(b"\x14"), ["CTRL-T"])
check("arrows", toks(b"\x1b[A\x1b[B\x1b[D"), ["UP", "DOWN", "LEFT"])
check("ss3 arrow", toks(b"\x1bOA"), ["UP"])
check("lone esc", toks(b"\x1b"), ["ESC"])
check("esc then key", toks(b"\x1bz"), ["ESC", "z"])
check("focus reports are not keys", toks(b"\x1b[I\x1b[O"), [])
check("mouse report is not a key", toks(b"\x1b[<0;9;3M"), [])
check("key survives surrounding reports", toks(b"\x1b[Iq\x1b[O"), ["q"])
check("a report storm yields no keys", toks(b"\x1b[I" * 200), [])
check("truncated sequence does not eat the next key",
      toks(b"\x1b[1;", b"x"), ["ESC", "[", "1", ";", "x"])

# Splits are healed once, in _read_chunk, so the parser above never has to
# time the byte stream -- which is what lets it be a pure function.
print("chunks arrive whole")

check("tail: incomplete csi", porter._tail_partial(b"x\x1b[1;"), True)
check("tail: complete csi", porter._tail_partial(b"x\x1b[1;2H"), False)
check("tail: bare esc", porter._tail_partial(b"\x1b"), True)
check("tail: esc plus a key", porter._tail_partial(b"\x1bz"), False)
check("tail: no esc at all", porter._tail_partial(b"abc"), False)

def chunked(*pieces):
    """_read_chunk against a keyboard handing over exactly these pieces."""
    q = list(pieces)
    with mock(porter, "_read_raw", lambda _t: q.pop(0) if q else b""):
        return porter._read_chunk(0.05)

check("whole sequence passes straight through", chunked(b"\x1b[A"), b"\x1b[A")
check("sequence split across reads is completed",
      chunked(b"\x1b[", b"A"), b"\x1b[A")
check("split right after esc is completed",
      chunked(b"\x1b", b"[B"), b"\x1b[B")
check("a lone esc is not waited on for ever", chunked(b"\x1b"), b"\x1b")
check("plain bytes need no completion", chunked(b"abc"), b"abc")
check("a healed split parses as one key",
      toks(chunked(b"\x1b[", b"A")), ["UP"])

# The one rule the blocking wait must keep: a wait that *fails* still costs
# time.  A dead console handle fails instantly, and a wait that returns
# instantly for ever is the same pegged core the old poll was.
print("a failed wait still costs time")

with mock(porter, "_wait_input", lambda _t: False):
    check("nothing ready means no bytes", porter._read_raw(0.01), b"")

if not porter.WINDOWS:
    slept = []
    def _boom(*a, **k): raise OSError("handle gone")
    with mock(porter.select, "select", _boom), \
         mock(porter.time, "sleep", slept.append):
        ready = porter._wait_input(1.0)
    check("a wait that cannot wait reports nothing ready", ready, False)
    check("... and does not come straight back", slept, [porter.DEAD_WAIT])

print("suspend recovery")

armed = []
with mock(porter, "arm_console", lambda: armed.append(1)):
    porter._TICK[0] = time.monotonic()
    porter._beat()
    check("a normal tick re-arms nothing", armed, [])
    porter._TICK[0] = time.monotonic() - porter.SUSPEND - 1
    porter._beat()
    check("a tick gap re-arms the console", armed, [1])

print("port watcher pacing")

def pacing(cost, interval=porter.FAST_SCAN):
    clock = _Clock(cost)
    slept = []
    watcher = porter.PortWatcher(porter.Bus())
    watcher.interval = interval
    def comports():
        clock.t += cost
        return []
    def waited(s):
        slept.append(s)
        raise _Stop
    watcher._quit.wait = waited
    with mock(porter.list_ports, "comports", comports), \
         mock(porter.time, "monotonic", clock.monotonic):
        try:
            watcher.run()
        except _Stop:
            pass
    return watcher.last_scan, slept[0]

cost, slept = pacing(0.01)
check("cheap scan sleeps the interval", (round(cost, 3), round(slept, 3)),
      (0.01, round(porter.FAST_SCAN, 3)))
cost, slept = pacing(0.5)
check("slow scan backs off to keep its share", (round(cost, 3), round(slept, 3)),
      (0.5, 4.0))
cost, slept = pacing(5.0)
check("backoff stays proportional for a very slow scan",
      (round(cost, 3), round(slept, 3)), (5.0, 40.0))

# What makes it event-driven rather than polled: the tick is private to the
# watcher, and only a *change* in the port set leaves the thread.
print("the watcher posts changes, not snapshots")

def posted(*snapshots):
    bus = porter.Bus()
    watcher = porter.PortWatcher(bus)
    seq = list(snapshots)
    def comports():
        if not seq:
            raise _Stop
        return seq.pop(0)
    watcher._quit.wait = lambda _s: None
    with mock(porter.list_ports, "comports", comports):
        try:
            watcher.run()
        except _Stop:
            pass
    out = []
    while True:
        kind, payload = bus.get(0)
        if kind is None:
            return out
        out.append(sorted(p.device for p in payload))

A, B = P("/dev/ttyA", 0x1, 0x2, "AA"), P("/dev/ttyB", 0x3, 0x4, "BB")
check("an unchanged port set posts once, not every tick",
      posted([A], [A], [A]), [["/dev/ttyA"]])
check("an arrival posts", posted([A], [A, B]),
      [["/dev/ttyA"], ["/dev/ttyA", "/dev/ttyB"]])
check("a departure posts", posted([A, B], [A]),
      [["/dev/ttyA", "/dev/ttyB"], ["/dev/ttyA"]])
check("an empty first scan still posts", posted([]), [[]])

# Enumeration failing means "unknown", not "everything unplugged".
def flaky():
    bus = porter.Bus()
    watcher = porter.PortWatcher(bus)
    seq = [[A], Exception, [A]]
    def comports():
        if not seq:
            raise _Stop
        nxt = seq.pop(0)
        if nxt is Exception:
            raise OSError("enumeration failed")
        return nxt
    watcher._quit.wait = lambda _s: None
    with mock(porter.list_ports, "comports", comports):
        try:
            watcher.run()
        except _Stop:
            pass
    out = []
    while True:
        kind, payload = bus.get(0)
        if kind is None:
            return out
        out.append(sorted(p.device for p in payload))

check("a failed enumeration reports nothing at all", flaky(), [["/dev/ttyA"]])

# Windows enumerates a USB-serial device as soon as the class driver claims
# it, but the COM number is a registry value the driver writes a moment
# later -- and pyserial reads that value without checking whether the read
# worked, so a device caught mid-attach comes back fully described with
# device=''.  Its identity is the *same* as the real entry about to appear,
# so left in the list it is an arrival for the very device porter is waiting
# for: porter opens port '', fails, and then never sees the real name land
# because the key never changed.  That is the "could not open port ''" loop.
print("a port with no name is not a port")

PHANTOM = P("", 0x239a, 0x80f4, "AAA", description="USB Serial Device")
REAL = P("COM7", 0x239a, 0x80f4, "AAA", description="USB Serial Device (COM7)")

check("the nameless entry and the real one are one device",
      porter._identity(PHANTOM), porter._identity(REAL))
check("a nameless port is dropped",
      [p.device for p in porter._named([PHANTOM, REAL])], ["COM7"])
check("a blank one too", porter._named([P("  ", 0x1, 0x2, "BB")]), [])
check("the watcher never publishes one, so the real name is still an arrival",
      posted([PHANTOM], [PHANTOM, REAL]), [[], ["COM7"]])

# _identity() is deliberately port-independent, so the same board on a
# different COM number is the same key -- and a diff on keys alone leaves the
# picker showing a port that no longer exists, with no event to correct it.
check("a port renamed under a stable key still posts",
      posted([REAL], [P("COM9", 0x239a, 0x80f4, "AAA")]),
      [["COM7"], ["COM9"]])

print("the bus")

bus = porter.Bus()
check("an empty bus times out", bus.get(0.01), (None, None))
bus.post(porter.KEY, b"q")
bus.post(porter.RX, b"hi")
check("events come back in order", [bus.get(0.01), bus.get(0.01)],
      [(porter.KEY, b"q"), (porter.RX, b"hi")])

# post_rx queues a signal only when one is not already outstanding, and only
# take_rx() clears that.  So a loop that takes an RX event off the queue and
# then returns without claiming the bytes mutes the bus for good -- every
# later post_rx sees a signal still outstanding and queues nothing.  That is a
# live port and a dead screen, which is what a disconnect/reconnect used to
# leave behind.
print("the rx signal is re-armed by taking the bytes")

bus = porter.Bus()
bus.post_rx(b"one")
check("first output signals", bus.get(0.01), (porter.RX, None))
bus.post_rx(b"two")
check("a signal already outstanding does not re-signal", bus.get(0.01),
      (None, None))
check("taking claims everything since the last take", bus.take_rx(), (b"onetwo", 0))
bus.post_rx(b"three")
check("and re-arms the signal", bus.get(0.01), (porter.RX, None))


print("a session hands the rx signal back however it ends")

LAST = b'last words\r\n'
def _noop(*a, **k): pass

for name, wind_up in (
        ("lost to a faulted reader",
         lambda b, d: b.post(porter.GONE, sess)),
        ("sent back to the picker",
         lambda b, d: b.post(porter.KEY, bytes([porter.PREFIX]) + b"d")),
        ("quit",
         lambda b, d: b.post(porter.KEY, bytes([porter.PREFIX]) + b"q"))):
    bus = porter.Bus()
    dev = porter.Device(port="COM9", key="239a:80f4:X", label="board",
                        baud=115200)
    sess = porter.Session(dev, bus)
    painted = []
    with mock(porter, "BUS", bus), mock(porter, "w", _noop):
        with mock(porter, "w_bytes", painted.append):
            # The device's last words and the event that ends the session
            # land in the same drain pass -- the normal case, since the read
            # that faults follows the read that returned data.
            bus.post_rx(LAST)
            wind_up(bus, dev)
            sess.run()
            check(f"{name}: the last words still reach the screen",
                  b"".join(painted), LAST)
            bus.post_rx(b"I am back")
            check(f"{name}: the next session's output still signals",
                  bus.get(0.01), (porter.RX, None))


# Coalescing bounds how much output is outstanding, not how often porter
# writes.  A terminal that keeps up is therefore the dangerous case: the loop
# free-runs, each pass takes back a few bytes, and porter is back to one
# write+flush per read -- which is what saturated a Windows console until
# flush() stopped returning and took the keyboard down with it.  Measured at
# 3123 passes/sec against a real board before this was bounded.
# close() says outright that it can leave a reader behind, and a leaked
# reader faults whenever its driver gets round to it -- long after the session
# that owned it is over.  Keyed by device it is indistinguishable from the
# live session's own reader, because a replug of the same board carries the
# same key.  That would kill a healthy session on every reconnect.
# A daemon thread that dies takes a whole faculty with it and leaves nothing
# on screen: a dead keyboard, or a device list frozen for the rest of the run.
print("a background thread survives what it runs into")

porter.FAULTS.clear()

class _Boom:
    """A port object that blows up where the watcher used to be unguarded."""
    device = "COM9"
    @property
    def vid(self): raise RuntimeError("backend went sideways")

watcher = porter.PortWatcher(porter.Bus())

def _one_scan():
    watcher._quit.set()                   # this pass is the only one
    return [_Boom()]

with mock(porter.list_ports, "comports", _one_scan):
    watcher.run()                         # must return, not raise
check("a scan that blows up in the diff does not kill the watcher",
      len(porter.FAULTS), 1)
check("... and says what happened", "backend went sideways" in porter.FAULTS[0],
      True)

porter.FAULTS.clear()
reader = porter.KeyReader(porter.Bus())
calls = [0]
def _explode(_timeout):
    calls[0] += 1
    if calls[0] >= 2:
        reader._quit.set()                # let it out on the second pass
    raise OSError("console handle went away")

with mock(porter, "_read_chunk", _explode), mock(porter, "DEAD_WAIT", 0.0):
    reader.run()                          # must return, not raise
check("a keyboard read that raises does not kill the reader", calls[0], 2)
check("... and each attempt is recorded", len(porter.FAULTS), 2)

porter.FAULTS.clear()


# The reader owns closing its own handle, so one that would not stop keeps the
# port open -- and the bare "access is denied" that follows reads like a
# broken device rather than something porter is still holding.
print("a port held by a leaked reader explains itself")

held = porter.Device(port="COM77", key="k", label="board", baud=115200)
alive = threading.Event()
stuck = threading.Thread(target=alive.wait, daemon=True, name="stuck-reader")
stuck.start()
porter._LEAKED[held.port] = stuck

check("a live leak is reported", porter._holder("COM77") is stuck, True)
check("an unrelated port is not", porter._holder("COM78"), None)

sess = porter.Session(held, porter.Bus())
with mock(porter, "_open_port", lambda d: (None, PermissionError("Access is denied"))):
    with mock(porter, "note", _noop):
        err = sess.open()
check("the failure names the holder", "did not stop when the device went" in err,
      True)
check("and still carries the underlying error", "Access is denied" in err, True)

alive.set(); stuck.join(timeout=2.0)
check("a leak that lets go is forgotten", porter._holder("COM77"), None)
check("... and stops being reported", porter._LEAKED.get("COM77"), None)


# The retry between open attempts is the one stretch with no session and no
# picker behind it.  A sleep there is felt as a lockup: keys pressed during
# it are not lost, they queue and land afterwards in the picker, where enter
# starts the open again.
print("the wait between open attempts is on the bus")

class _Events:
    """Bus stand-in that hands out queued events, then nothing."""
    def __init__(self, *events): self.events, self.gets = list(events), 0
    def get(self, timeout):
        self.gets += 1
        return self.events.pop(0) if self.events else (None, None)

with mock(porter, "BUS", _Events((porter.KEY, b"\x1b"))):
    check("esc gives up on a retrying open", porter._wait_bus(9.0), True)
with mock(porter, "BUS", _Events((porter.KEY, b"\x03"))):
    check("so does ctrl-c", porter._wait_bus(9.0), True)

started = time.monotonic()
with mock(porter, "BUS", _Events((porter.KEY, b"x"), (porter.KEY, b"\r"))):
    check("any other key is dropped, not queued", porter._wait_bus(0.05), False)
check("... and the wait still costs its time, or a caller is a spin",
      time.monotonic() - started >= 0.05, True)

# A reader leaked from an earlier session may still be posting.  Claiming the
# signal here would re-arm it every pass and free-run this loop at the
# reader's rate -- the same rule as the loops that own the screen.
bus = porter.Bus()
bus.post_rx(b"boot log")
with mock(porter, "BUS", bus):
    porter._wait_bus(0.02)
check("device output is left outstanding, not claimed", bus.take_rx(),
      (b"boot log", 0))

class _Shut:
    """serial.Serial stand-in whose port never opens."""
    def open(self): raise OSError("could not open port")

gone = porter.Device(port="COM5", key="239a:80f4:AAA", label="board", baud=115200)
said = []
with mock(porter, "serial", types.SimpleNamespace(Serial=_Shut)), \
     mock(porter, "_wait_bus", lambda _s: True), mock(porter, "note", said.append):
    ser, err = porter._open_port(gone)
check("a cancelled wait ends the retries", (ser, err is porter.CANCELLED),
      (None, True))
check("... after one attempt, not ten", len(said), 1)

sess = porter.Session(gone, porter.Bus())
with mock(porter, "_open_port", lambda d: (None, porter.CANCELLED)), \
     mock(porter, "note", _noop):
    check("and giving up is not reported as a failure", sess.open(),
          "gave up opening COM5")


print("a session dies only on its own reader's fault")

bus = porter.Bus()
dev = porter.Device(port="COM9", key="239a:80f4:X", label="board", baud=115200)
dead = porter.Session(dev, bus)         # the session that was lost
live = porter.Session(dev, bus)         # its replacement, same board, same key

def _quit_soon():
    time.sleep(0.25)
    bus.post(porter.KEY, bytes([porter.PREFIX]) + b"q")

with mock(porter, "BUS", bus), mock(porter, "w", _noop):
    with mock(porter, "w_bytes", _noop):
        bus.post(porter.GONE, dead)     # the leak finally faults
        threading.Thread(target=_quit_soon, daemon=True).start()
        reason = live.run()

check("a leaked reader's fault does not end the live session",
      reason, porter.QUIT)
check("... and does not mark it lost", live.lost, False)

bus = porter.Bus()
live = porter.Session(dev, bus)
with mock(porter, "BUS", bus), mock(porter, "w", _noop):
    with mock(porter, "w_bytes", _noop):
        bus.post(porter.GONE, live)
        check("its own reader's fault still ends it", live.run(), porter.LOST)
check("... and marks it lost", live.lost, True)


print("a flood is painted at a bounded rate, not once per read")

BPS = 115200 // 10          # ~11.5 KB/s, one 8N1 byte per 10 bits
CHUNK = 4                   # what a read() hands back on a live line
RUN = 0.5

bus = porter.Bus()
dev = porter.Device(port="COM9", key="239a:80f4:X", label="board", baud=115200)
sess = porter.Session(dev, bus)
writes, sent = [], [0]
flooding = threading.Event()
flooding.set()

def _flood():
    # sleep(), not a spin.  A real reader sits in ReadFile with the GIL
    # released, and that is what lets the main loop free-run between
    # arrivals; a spinning producer holds the GIL and hides the bug outright.
    while flooding.is_set():
        bus.post_rx(b"x" * CHUNK)
        sent[0] += CHUNK
        time.sleep(CHUNK / BPS)

def _stopper():
    time.sleep(RUN)
    bus.post(porter.KEY, bytes([porter.PREFIX]) + b"q")

with mock(porter, "BUS", bus), mock(porter, "w", _noop):
    with mock(porter, "w_bytes", lambda d: writes.append(len(d))):
        threading.Thread(target=_flood, daemon=True).start()
        threading.Thread(target=_stopper, daemon=True).start()
        started = time.monotonic()
        reason = sess.run()
        elapsed = time.monotonic() - started
        flooding.clear()

rate = len(writes) / elapsed
chunk = sum(writes) / len(writes)
# Absolute, not derived from PAINT_MIN: a ceiling that moves with the thing
# under test cannot fail.  60/s is the design rate; a real console died at
# ~3000/s, so anything under 300 is comfortably safe and comfortably strict.
ceiling = 300

check("the keystroke still lands under a flood", reason, porter.QUIT)
check(f"the write rate stays bounded ({rate:.0f}/s, ceiling {ceiling:.0f}/s)",
      rate < ceiling, True)
check(f"and the chunks stay fat ({chunk:.0f} bytes, not {CHUNK})",
      chunk > 4 * CHUNK, True)
check("no output is lost on the way", sum(writes), sent[0])


# The picker and a page own the screen, so held output never reaches the
# console and cannot saturate it -- but claiming the RX signal the moment it
# arrives re-arms it, the reader posts again at once, and the loop free-runs
# at the reader's rate for no benefit.  Caught at 316 passes/sec with a page
# open over a chatty board.  SPIN exists to say no loop belongs there.
print("a loop that owns the screen does not spin on a chatty device")

BPS = 115200 // 10
CHUNK = 4

def _flooded(run_loop, secs=0.4):
    """Run `run_loop` against a wire-paced device; return passes/sec."""
    bus = porter.Bus()
    flooding = threading.Event()
    flooding.set()

    def _flood():
        while flooding.is_set():
            bus.post_rx(b"x" * CHUNK)
            time.sleep(CHUNK / BPS)

    def _stopper():
        time.sleep(secs)
        bus.post(porter.KEY, b"q")

    screen = porter.Screen()
    screen.hold()
    with mock(porter, "BUS", bus), mock(porter, "SCREEN", screen):
        with mock(porter, "w", _noop), mock(porter, "w_bytes", _noop):
            threading.Thread(target=_flood, daemon=True).start()
            threading.Thread(target=_stopper, daemon=True).start()
            porter._TICK[1] = 0
            started = time.monotonic()
            run_loop(bus)
            elapsed = time.monotonic() - started
    flooding.clear()
    return porter._TICK[1] / elapsed, bus

def _page_loop(bus):
    porter._page("body", timeout=5.0)

rate, bus = _flooded(_page_loop)
check(f"a page waits rather than spins ({rate:.0f}/s, spin is {porter.SPIN}/s)",
      rate < porter.SPIN, True)
bus.post_rx(b"more")
check("a page re-arms the signal on the way out", bus.get(0.01),
      (porter.RX, None))

def _picker_loop(bus):
    with mock(porter, "_scan_ports", lambda: []):
        porter.picker(configparser.ConfigParser(), pathlib.Path("nowhere"),
                      {}, None, None)

rate, bus = _flooded(_picker_loop)
check(f"the picker waits rather than spins ({rate:.0f}/s)",
      rate < porter.SPIN, True)


print("the picker takes back the device whose session was lost")

# The one thing porter reconnects to on its own, and only here: same device,
# same visit to the picker, and only on the edge where it *appears*.
_A = P("/dev/a", 0x239a, 0x80f4, "AAA", description="board A")
_B = P("/dev/b", 0x0403, 0x6001, "BBB", description="board B")

class _Plug:
    """Bus stand-in: the first get() plugs one port in, the next ends the loop."""
    def __init__(self, ports, arriving):
        self.ports, self.arriving, self.gets = ports, arriving, 0
    def get(self, timeout=None):
        self.gets += 1
        if self.gets > 1:
            _stop()
        self.ports.append(self.arriving)
        return porter.PORTS, list(self.ports)
    def take_rx(self):
        return b"", 0

def _on_plug(present, arriving, awaiting):
    """What the picker does when `arriving` turns up while it is on screen."""
    ports = list(present)
    with mock(porter, "_scan_ports", lambda: list(ports)),          mock(porter, "BUS", _Plug(ports, arriving)),          mock(porter, "w", _noop), mock(porter, "w_bytes", _noop):
        try:
            return porter.picker(configparser.ConfigParser(),
                                 pathlib.Path("nowhere"), {}, None, None,
                                 awaiting=awaiting)
        except _Stop:
            return None

got = _on_plug([], _A, "239a:80f4:AAA")
check("the awaited device is returned the moment it appears",
      got and got.port, "/dev/a")
check("a different device is not",
      _on_plug([], _B, "239a:80f4:AAA"), None)
check("and with nothing awaited, nothing is taken",
      _on_plug([], _A, None), None)
# Presence is not enough: a device already listed when the picker opened was
# not plugged in during this visit, so a failed open lands back here and
# waits rather than retrying in a loop.
check("a device already in the list is not an arrival",
      _on_plug([_A], _B, "239a:80f4:AAA"), None)


print("the screen holds device output while porter owns it")

written = []
scr = porter.Screen()
with mock(porter, "w_bytes", written.append):
    scr.feed(b"a")
    check("unheld output goes straight out", written, [b"a"])
    scr.hold()
    scr.feed(b"bc")
    check("held output does not", written, [b"a"])
    check("releasing hands back what was held", scr.release(), b"bc")
    scr.hold(); scr.hold()
    scr.feed(b"d")
    check("a nested hold keeps holding", scr.release(), b"")
    check("... until the last one lifts", scr.release(), b"d")
    check("an unbalanced release cannot go negative", scr.release(), b"")

scr = porter.Screen()
scr.hold()
scr.feed(b"x" * (porter.BACKLOG_MAX + 500))
check("the hold is bounded, and keeps the newest",
      len(scr.release()), porter.BACKLOG_MAX)

print()
if fails:
    print("FAILURES:"); [print(" ", f) for f in fails]; sys.exit(1)
print("all unit checks passed")
