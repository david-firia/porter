"""Run porter with comports() faked onto two socat PTYs.
Presence of /tmp/porter_present controls whether device A is 'plugged in'."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import porter

FLAG = pathlib.Path("/tmp/porter_present")
FLAG_B = pathlib.Path("/tmp/porter_present_b")   # second device, absent when unset

class Dev:
    location = None
    def __init__(s, device, vid, pid, sn, desc):
        s.device, s.vid, s.pid, s.serial_number, s.description = device, vid, pid, sn, desc

A = Dev("/tmp/porter_a", 0x239a, 0x80f4, "TESTBOARD1", "CircuitPython")
B = Dev("/tmp/porter_c", 0x0403, 0x6001, "SECOND", "FTDI second")
C = Dev("COM99", None, None, None, "Standard Serial over Bluetooth")

porter.list_ports.comports = lambda: (([A] if FLAG.exists() else [])
                                      + ([B] if FLAG_B.exists() else []) + [C])
sys.exit(porter.main(sys.argv[1:]))
