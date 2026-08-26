"""Repeated unplug/replug cycles -- the sequence that hung on Windows.
Also watches the process thread count for orphaned reader threads."""
import os, pty, re, subprocess, sys, time, pathlib, fcntl, termios, struct

HERE = os.path.dirname(os.path.abspath(__file__))
FLAG = pathlib.Path("/tmp/porter_present"); CFG = "/tmp/porter_stress_config"
FLAG_B = pathlib.Path("/tmp/porter_present_b")
pathlib.Path(CFG).unlink(missing_ok=True); FLAG.write_text(""); FLAG_B.write_text("")

socats = [subprocess.Popen(["socat", f"pty,raw,echo=0,link=/tmp/porter_{a}",
                            f"pty,raw,echo=0,link=/tmp/porter_{b}"], stderr=subprocess.DEVNULL)
          for a, b in (("a","b"), ("c","d"))]
for _ in range(100):
    if all(os.path.exists(f"/tmp/porter_{x}") for x in "abc"): break
    time.sleep(0.05)

master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
proc = subprocess.Popen([sys.executable, "-u", f"{HERE}/wrapper.py", "-c", CFG],
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
def expect(name, pat, timeout=15.0):
    rx = re.compile(pat.encode()); end = time.monotonic()+timeout
    while time.monotonic() < end:
        pump(0.1)
        m = rx.search(bytes(buf), CUR[0])
        if m:
            CUR[0] = m.end(); oks.append(name); print(f"  ok  {name}"); return True
    fails.append(name); print(f"  FAIL {name}: no {pat!r} after {CUR[0]}"); return False
def record(name, ok, extra=""):
    (oks if ok else fails).append(name)
    print(("  ok  " if ok else "  FAIL ")+name+(f" ({extra})" if extra else ""))
def threads():
    try:
        return int(re.search(r"Threads:\s+(\d+)",
                   open(f"/proc/{proc.pid}/status").read()).group(1))
    except Exception: return -1

expect("initial picker", r"q quit")
os.write(master, b"\r")
expect("connected", r"CircuitPython on /tmp/porter_a @ 115200")
time.sleep(1.0)
base = threads()
print(f"  ..  baseline threads = {base}")

N = 6
for i in range(1, N+1):
    print(f"cycle {i}")
    FLAG.unlink()
    if not expect(f"  disconnect {i}", r"CircuitPython disconnected"): break
    time.sleep(0.4)
    FLAG.write_text("")
    # Losing the device landed in the picker, and the device it lost is the
    # one thing porter takes back on its own -- so the replug above is the
    # whole reconnect, and this exercises that path once per cycle.
    if not expect(f"  reconnect {i}", r"CircuitPython on /tmp/porter_a @ 115200"): break
    time.sleep(0.3)

after = threads()
record("no orphan threads", 0 < after <= base+1, f"baseline {base} -> {after}")

# still responsive and still pumping data after all that churn
dev = os.open("/tmp/porter_b", os.O_RDWR | os.O_NOCTTY); os.set_blocking(dev, False)
os.write(dev, b"still alive\r\n"); time.sleep(0.4)
expect("data flows after churn", r"still alive")
os.write(master, b"\x14c")
expect("ctrl-t still responsive", r"239a:80f4:TESTBOARD1")
os.write(master, b"\r")            # the config page is modal; close it
expect("config page closes", r"\x1b\[\?1049l")

# A device that will not shut up, against a terminal that cannot keep up.
# This is the shape that locked porter solid on Windows: one bus event per
# read became one console write per read, the writes backed up, and the
# keyboard went with them because painting is on the main thread.
print("a chatty device against a slow terminal")
blaster = subprocess.Popen(
    [sys.executable, "-c",
     "import os\n"
     "fd = os.open('/tmp/porter_b', os.O_RDWR | os.O_NOCTTY)\n"
     "b = b'noise ' * 200\n"
     "while True:\n"
     "    try: os.write(fd, b)\n"
     "    except OSError: break\n"],
    stderr=subprocess.DEVNULL)

# Read slowly, so porter's stdout genuinely backs up.
end = time.monotonic() + 5.0
while time.monotonic() < end:
    try: buf.extend(os.read(master, 2048))
    except BlockingIOError: pass
    except OSError: break
    time.sleep(0.10)

mark = len(buf)
t0 = time.monotonic()
os.write(master, b"\x14c")                  # ask for the config page
answered = None
while time.monotonic() - t0 < 15.0:
    try:
        d = os.read(master, 1 << 20)          # now drain at full speed
        if d: buf.extend(d)
    except BlockingIOError: pass
    except OSError: break
    if b"\x1b[?1049h" in bytes(buf[mark:]):
        answered = time.monotonic() - t0
        break
    time.sleep(0.002)
blaster.kill(); blaster.wait()
record("keyboard survives a flood", answered is not None and answered < 5.0,
       f"{answered:.2f}s" if answered else "never answered")
# Falling behind is allowed; doing it silently is not.
record("dropped output is admitted to",
       b"fell behind; dropped" in bytes(buf[mark-200000 if mark > 200000 else 0:]))
CUR[0] = len(buf)
os.write(master, b"\r")                      # close the page
expect("page closes after the flood", r"\x1b\[\?1049l")
pump(0.5)

# hard failure: yank the pty out from under the reader
print("socat killed mid-session")
socats[0].kill(); socats[0].wait()
expect("survives pty death", r"disconnected|cannot open", timeout=15)

os.write(master, b"q")
try: rc = proc.wait(timeout=10)
except subprocess.TimeoutExpired: proc.kill(); rc = "HUNG"
record("clean exit", rc == 0, f"rc={rc}")

for s in socats: s.kill()
print(f"\n{len(oks)} passed, {len(fails)} failed")
if fails:
    pathlib.Path(f"{HERE}/stress-capture.bin").write_bytes(bytes(buf))
    print("FAILURES: " + ", ".join(fails)); sys.exit(1)
