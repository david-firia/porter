"""Interactive behaviour: esc-resume, alias naming, non-USB filtering."""
import os, pty, re, subprocess, sys, time, pathlib, fcntl, termios, struct

HERE = os.path.dirname(os.path.abspath(__file__))
FLAG = pathlib.Path("/tmp/porter_present"); CFG = pathlib.Path("/tmp/porter_ui_config")
CFG.unlink(missing_ok=True); FLAG.write_text("")

socats = [subprocess.Popen(["socat", f"pty,raw,echo=0,link=/tmp/porter_{a}",
                            f"pty,raw,echo=0,link=/tmp/porter_{b}"], stderr=subprocess.DEVNULL)
          for a, b in (("a","b"), ("c","d"))]
for _ in range(100):
    if all(os.path.exists(f"/tmp/porter_{x}") for x in "abc"): break
    time.sleep(0.05)

master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
proc = subprocess.Popen([sys.executable, "-u", f"{HERE}/wrapper.py", "-c", str(CFG)],
                        stdin=slave, stdout=slave, stderr=slave, close_fds=True)
os.close(slave); os.set_blocking(master, False)

buf = bytearray(); CUR = [0]; oks, fails = [], []
def pump(t=0.15):
    end = time.monotonic()+t
    while time.monotonic() < end:
        try:
            d = os.read(master, 65536)
            if d: buf.extend(d)
        except BlockingIOError: time.sleep(0.01)
        except OSError: break
def expect(name, pat, timeout=10.0):
    rx = re.compile(pat.encode()); end = time.monotonic()+timeout
    while time.monotonic() < end:
        pump(0.1)
        m = rx.search(bytes(buf), CUR[0])
        if m: CUR[0] = m.end(); oks.append(name); print(f"  ok  {name}"); return True
    fails.append(name); print(f"  FAIL {name}: no {pat!r} after {CUR[0]}"); return False
def record(name, ok, extra=""):
    (oks if ok else fails).append(name)
    print(("  ok  " if ok else "  FAIL ")+name+(f" ({extra})" if extra else ""))
def send(d, wait=0.35): os.write(master, d if isinstance(d,bytes) else d.encode()); time.sleep(wait)
def frame():
    pump(0.5); return bytes(buf).split(b"\x1b[H")[-1].decode("utf8","replace")

print("non-USB ports hidden in the picker")
expect("picker up", r"q quit")
f = frame()
record("bluetooth port hidden", "COM99" not in f)
record("2 usb devices shown", "2 devices" in f, f"saw {'2 devices' in f}")
record("aliases path shown", "/tmp/porter_ui_config" in f)
record("no resume hint without a session", "esc resume" not in f)
CUR[0] = len(buf)

print("esc with no session quits")
send("\x1b", 1.0)
record("still running (esc must not quit... )", proc.poll() is None) if False else None
# esc with no session should exit
try: rc0 = proc.wait(timeout=4); record("esc exits when nothing to resume", rc0 == 0, f"rc={rc0}")
except subprocess.TimeoutExpired: record("esc exits when nothing to resume", False, "still running"); proc.kill()

# ---- restart for the session tests ----
master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
proc = subprocess.Popen([sys.executable, "-u", f"{HERE}/wrapper.py", "-c", str(CFG)],
                        stdin=slave, stdout=slave, stderr=slave, close_fds=True)
os.close(slave); os.set_blocking(master, False)
buf.clear(); CUR[0] = 0

print("esc from picker returns to the session")
expect("picker up again", r"q quit")
send("\r")
expect("connected", r"CircuitPython on /tmp/porter_a")
dev = os.open("/tmp/porter_b", os.O_RDWR | os.O_NOCTTY); os.set_blocking(dev, False)
os.write(dev, b"before picker\r\n"); time.sleep(0.3)
expect("data before picker", r"before picker")

send(b"\x14d")                                  # ctrl-t d -> picker
expect("picker from session", r"q quit")
f = frame()
record("resume hint shown", "esc resume" in f)
CUR[0] = len(buf)
os.write(dev, b"DURING PICKER\r\n"); time.sleep(0.5)   # arrives while paused
f2 = frame()
record("picker not corrupted by device output", "DURING PICKER" not in f2)

send("\x1b")                                    # esc -> back to session
expect("resumed, not exited", r"back on CircuitPython")
expect("buffered output flushed", r"DURING PICKER")
record("process still alive", proc.poll() is None)
os.write(dev, b"after resume\r\n"); time.sleep(0.3)
expect("session still live", r"after resume")

print("naming a device")
send(b"\x14d"); expect("picker for naming", r"q quit")
send("a"); expect("name prompt", r"name for this device:")
send("CodeBot #7", 0.6)
expect("typed name echoed", r"CodeBot #7")
send("\r")
expect("alias saved", r"saved as \[CodeBot #7\]")
f3 = frame()
record("label now shows the alias", "CodeBot #7" in f3)
text = CFG.read_text()
record("written to the config file", "[CodeBot #7]" in text and "239a:80f4:TESTBOARD1" in text)
CUR[0] = len(buf)

print("renaming it")
send("a"); expect("prompt prefilled for rename", r"name for this device: CodeBot #7")
for _ in range(10): send("\x7f", 0.06)          # backspace over the name
send("bench-board"); send("\r")
expect("renamed", r"renamed to \[bench-board\]")
t2 = CFG.read_text()
record("config renamed in place", "[bench-board]" in t2 and "[CodeBot #7]" not in t2)
record("id survived rename", "239a:80f4:TESTBOARD1" in t2)

print("esc cancels the prompt")
CUR[0] = len(buf)
send("a"); expect("prompt again", r"name for this device:")
send("\x1b"); expect("prompt cancelled", r"cancelled")
record("still in picker after cancel", proc.poll() is None)

print("high contrast")
CUR[0] = len(buf)
send("H")
# set_theme writes the OSC before the picker redraws with the new message.
expect("terminal told the new background", r"\x1b\]11;#000000\x07")
expect("picker reports the theme", r"theme: contrast-dark")

send("\x1b"); expect("back in the session", r"back on")
CUR[0] = len(buf)
os.write(dev, b"\x1b[31mSCARLET\x1b[0m\r\n"); pump(0.5)
i = bytes(buf).find(b"SCARLET", CUR[0])
record("device colour flattened in high contrast",
       i > 0 and b"\x1b[31m" not in bytes(buf)[CUR[0]:i], f"at {i}")

CUR[0] = len(buf)
send(b"\x14H"); expect("cycles to the light theme", r"theme: contrast-light")
send(b"\x14H")
expect("terminal colours handed back", r"\x1b\]110\x07\x1b\]111\x07")
expect("cycles back to default", r"theme: default")
CUR[0] = len(buf)
os.write(dev, b"\x1b[31mCRIMSON\x1b[0m\r\n"); pump(0.5)
j = bytes(buf).find(b"CRIMSON", CUR[0])
record("device colour restored under the default theme",
       j > 0 and b"\x1b[31m" in bytes(buf)[CUR[0]:j], f"at {j}")

send(b"\x14d"); expect("picker for exit", r"q quit")

send("q")
try: rc = proc.wait(timeout=8)
except subprocess.TimeoutExpired: proc.kill(); rc = "HUNG"
record("clean exit", rc == 0, f"rc={rc}")

for s_ in socats: s_.kill()
print(f"\n{len(oks)} passed, {len(fails)} failed")
if fails:
    pathlib.Path(f"{HERE}/ui-capture.bin").write_bytes(bytes(buf))
    print("FAILURES: " + ", ".join(fails)); sys.exit(1)
