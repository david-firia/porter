"""Unit tests for porter's pure logic -- no tty, no hardware."""
import sys, types, tempfile, pathlib, configparser, contextlib, io
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

print()
if fails:
    print("FAILURES:"); [print(" ", f) for f in fails]; sys.exit(1)
print("all unit checks passed")
