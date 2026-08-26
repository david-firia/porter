import os, pty, re, subprocess, sys, time, pathlib, signal

SCRATCH = os.path.dirname(os.path.abspath(__file__))
FLAG = pathlib.Path("/tmp/porter_present")
FLAG_B = pathlib.Path("/tmp/porter_present_b")
CFG = "/tmp/porter_test_config"
for f in (CFG,): pathlib.Path(f).unlink(missing_ok=True)
FLAG.write_text(""); FLAG_B.write_text("")

socats = [
    subprocess.Popen(["socat","-u0","pty,raw,echo=0,link=/tmp/porter_a","pty,raw,echo=0,link=/tmp/porter_b"],
                     stderr=subprocess.DEVNULL),
    subprocess.Popen(["socat","pty,raw,echo=0,link=/tmp/porter_c","pty,raw,echo=0,link=/tmp/porter_d"],
                     stderr=subprocess.DEVNULL),
]
# -u0 isn't valid; recreate properly
for s in socats: s.kill()
socats = [subprocess.Popen(["socat","pty,raw,echo=0,link=/tmp/porter_a","pty,raw,echo=0,link=/tmp/porter_b"],
                           stderr=subprocess.DEVNULL),
          subprocess.Popen(["socat","pty,raw,echo=0,link=/tmp/porter_c","pty,raw,echo=0,link=/tmp/porter_d"],
                           stderr=subprocess.DEVNULL)]
for _ in range(100):
    if all(os.path.exists(p) for p in ("/tmp/porter_a","/tmp/porter_b","/tmp/porter_c")): break
    time.sleep(0.05)

master, slave = pty.openpty()
import termios, struct, fcntl
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
proc = subprocess.Popen([sys.executable, "-u", os.path.join(SCRATCH,"wrapper.py"), "-c", CFG],
                        stdin=slave, stdout=slave, stderr=slave, close_fds=True)
os.close(slave)
os.set_blocking(master, False)

buf = bytearray()
fails, oks = [], []

def pump(t=0.2):
    end = time.monotonic()+t
    while time.monotonic() < end:
        try:
            d = os.read(master, 65536)
            if d: buf.extend(d)
        except BlockingIOError: time.sleep(0.01)
        except OSError: break

CURSOR = [0]
def expect(name, pat, timeout=6.0):
    end = time.monotonic()+timeout
    rx = re.compile(pat.encode() if isinstance(pat,str) else pat)
    while time.monotonic() < end:
        pump(0.1)
        m = rx.search(bytes(buf), CURSOR[0])
        if m:
            CURSOR[0] = m.end(); oks.append(name); print(f"  ok  {name}"); return True
    fails.append(f"{name}: never saw {pat!r} after offset {CURSOR[0]}")
    print(f"  FAIL {name}: never saw {pat!r} after offset {CURSOR[0]}")
    return False

def absent(name, pat, wait=2.0):
    pump(wait)
    if re.search(pat.encode(), bytes(buf)[-4000:]):
        fails.append(f"{name}: unexpectedly saw {pat!r}"); print(f"  FAIL {name}"); return False
    oks.append(name); print(f"  ok  {name}"); return True

def send(data):
    os.write(master, data if isinstance(data,bytes) else data.encode())
    time.sleep(0.25)

def mark(): return len(buf)
def since(n): return bytes(buf[n:])

dev = os.open("/tmp/porter_b", os.O_RDWR | os.O_NOCTTY)
os.set_blocking(dev, False)
def dev_write(s): os.write(dev, s.encode() if isinstance(s,str) else s); time.sleep(0.3)
def dev_read(t=1.0):
    end=time.monotonic()+t; out=bytearray()
    while time.monotonic()<end:
        try: out.extend(os.read(dev, 4096))
        except BlockingIOError: time.sleep(0.02)
        except OSError: break
    return bytes(out)

print("picker")
expect("picker renders", r"q quit")          # first full frame drawn
pump(0.5)
frames = bytes(buf).split(b"\x1b[H")
frame = frames[-1].decode("utf8","replace")
for nm, pat in [("device A listed", r"CircuitPython"), ("device B listed", r"FTDI second"),
                ("2 devices", r"2 devices"), ("footer", r"enter connect"),
                ("selection marker on A", r">\s+1\s+CircuitPython"),
                ("B not selected", r"\s+2\s+FTDI second")]:
    ok = re.search(pat, frame)
    (oks if ok else fails).append(nm)
    print(("  ok  " if ok else "  FAIL ")+nm)
CURSOR[0] = len(buf)

print("connect")
send("\r")
expect("session banner", r"CircuitPython on /tmp/porter_a @ 115200")

print("data flow")
n = mark(); dev_write("hello from device\r\n")
expect("device -> screen", r"hello from device")
dev_read(0.3)
send("ping\r")
got = dev_read()
(oks if got==b"ping\r" else fails).append("keyboard -> device")
print(("  ok  " if got==b"ping\r" else "  FAIL ")+f"keyboard -> device (got {got!r})")

print("ctrl-t commands")
send(b"\x14?");  expect("help page opens", r"\x1b\[\?1049h")
expect("help text", r"ctrl-t d\s+back to device picker")
send("\r");      expect("help page closes", r"\x1b\[\?1049l")
send(b"\x14c");  expect("show config", r"239a:80f4:TESTBOARD1")
send("\r");      expect("config page closes", r"\x1b\[\?1049l")
send(b"\x14L");  expect("line states (pty: no modem lines)", r"cts=|line states unavailable")
send(b"\x14b");  expect("break sent", r"break sent")
dev_read(0.3)
send(b"\x14\x14")
got = dev_read()
(oks if got==b"\x14" else fails).append("literal ctrl-t")
print(("  ok  " if got==b"\x14" else "  FAIL ")+f"literal ctrl-t (got {got!r})")

send(b"\x14e"); expect("echo on", r"local echo on")
n = mark(); send("XY"); pump(0.4)
ok = b"XY" in since(n)
(oks if ok else fails).append("local echo works")
print(("  ok  " if ok else "  FAIL ")+"local echo works")
# XY also dismissed the "local echo on" ack -- a key that takes back a toast
# must still reach the device, or porter has stopped being a transparent pipe.
got = dev_read()
ok = got == b"XY"
(oks if ok else fails).append("key that dismisses an ack still reaches the device")
print(("  ok  " if ok else "  FAIL ")+f"key that dismisses an ack still reaches the device (got {got!r})")
send(b"\x14e"); expect("echo off", r"local echo off")

print("unplug -> the picker")
n = mark()
FLAG.unlink()
expect("disconnect detected", r"CircuitPython disconnected", timeout=8)
# Losing a device always lands in the picker, which is already a live device
# monitor -- so whatever else is plugged in stays visible and selectable.
ok = b"\x1b[?1049h" in since(n)
(oks if ok else fails).append("loss lands in the picker")
print(("  ok  " if ok else "  FAIL ")+"loss lands in the picker")
expect("the other device is still listed", r"FTDI second", timeout=5)

# The one thing porter reconnects to on its own: the device whose session it
# just lost, appearing again during this same visit to the picker.  Plug it
# back in and you are on it, with no keystroke at all.
FLAG.write_text("")
expect("the lost device is taken back on sight",
       r"CircuitPython on /tmp/porter_a @ 115200", timeout=8)
pump(0.8)

print("a second device stays reachable while one is missing")
FLAG.unlink()
expect("back to the picker", r"enter connect", timeout=8)
# The old auto-reconnect waited in place watching only the device it had
# lost, so a second device plugged in during the wait was invisible.
FLAG_B.unlink(); pump(1.5)
FLAG_B.write_text("")
expect("second device appears while waiting", r"FTDI second", timeout=8)
# ... and is not connected to: only the lost device comes back by itself.
absent("a different device still waits to be picked",
       r"FTDI second on /tmp/porter_c")
send("\r"); expect("and can be connected to", r"FTDI second on /tmp/porter_c", timeout=8)
FLAG.write_text(""); pump(1.5)
send(b"\x14d"); pump(0.8)
send("1"); expect("back on A for what follows", r"CircuitPython on /tmp/porter_a", timeout=8)
pump(0.8)

print("ctrl-t n / ctrl-t d")
send(b"\x14n"); expect("next device", r"FTDI second on /tmp/porter_c")
send(b"\x14d"); expect("back to picker", r"enter connect", timeout=5)

print("hotplug in picker")
FLAG.unlink()
pump(1.5)
n = mark(); pump(1.0)
ok = b"1 device" in since(n) or b"1 device" in bytes(buf[-3000:])
(oks if ok else fails).append("picker drops unplugged device")
print(("  ok  " if ok else "  FAIL ")+"picker drops unplugged device")
FLAG.write_text("")
expect("picker shows +new on replug", r"\+new", timeout=5)

print("a device lost while the picker is up")
send("\r"); expect("on A again", r"CircuitPython on /tmp/porter_a", timeout=8)
send(b"\x14d"); expect("picker over a live session", r"esc resume", timeout=5)
# Kill the pty, not the presence flag: that faults the reader at once, which
# is the only way the loss reaches porter while the picker owns the screen.
socats[0].kill(); socats[0].wait()
pump(1.5)
n = mark()
send("\x1b")                                      # esc means "never mind"
# ... and must not mean quit just because the device went.  Resuming a lost
# session reports the disconnect and lands straight back in the picker.
expect("esc reports the disconnect", r"CircuitPython disconnected", timeout=8)
ok = proc.poll() is None
(oks if ok else fails).append("esc after a loss does not quit porter")
print(("  ok  " if ok else "  FAIL ")+"esc after a loss does not quit porter")
expect("and lands back in the picker", r"enter connect", timeout=8)

print("quit")
send("q")
try: rc = proc.wait(timeout=6)
except subprocess.TimeoutExpired: proc.kill(); rc = "timeout"
(oks if rc==0 else fails).append("clean exit")
print(("  ok  " if rc==0 else "  FAIL ")+f"clean exit (rc={rc})")

for s in socats: s.kill()
os.close(dev)
print(f"\n{len(oks)} passed, {len(fails)} failed")
if fails:
    print("FAILURES:"); [print("  -",f) for f in fails]
    pathlib.Path(f"{SCRATCH}/integ-capture.bin").write_bytes(bytes(buf))
    print(f"(full capture in {SCRATCH}/integ-capture.bin)")
    sys.exit(1)
