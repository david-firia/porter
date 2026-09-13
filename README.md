# porter

A serial terminal with a live device picker. One file, `pyserial` the only dependency.

Built for the workflow of cycling between a drawer full of USB serial boards: plug one
in, it is already selected; unplug it, porter waits and grabs it again when it returns.

    uv tool install --editable .   # see Install below for the full recipe
    porter

## Install

`porter` is a single file, so anything that puts it on PATH works. The recommended
route is `uv tool install`, which builds an isolated venv holding `pyserial` and drops
a real `porter` executable on PATH. Your system Python stays untouched, and if you have
no suitable Python at all, uv fetches a managed one into the tool venv.

`--editable` throughout means the command points at your working copy: edits to
`porter.py` take effect on the next run with no reinstall. **Keep the source folder
somewhere permanent — moving or deleting it breaks the command.**

### Windows

Install uv if you do not have it, then **open a new terminal**:

```powershell
winget install --id=astral-sh.uv -e
```

Then run the installer from the folder holding `porter.py`:

```powershell
cd C:\path\to\porter
.\install.ps1
```

Double-clicking `install.cmd` does the same thing without arguing with your
PowerShell execution policy. Either way it installs the command, puts it on PATH,
registers the Windows Terminal profile, warns about the `.PY`-on-PATH trap below,
and runs `porter --list` to prove it worked.

| Switch | |
|---|---|
| `-SkipTerminalProfile` | install the command only, leave the dropdown alone |
| `-AllUsers` | register the terminal profile machine-wide; needs an elevated prompt |
| `-Uninstall` | remove both the command and the profile |

**Restart the terminal afterwards** — PATH is read at process start.

#### By hand

```powershell
cd C:\path\to\porter
uv tool install --editable .
uv tool update-shell
```

`update-shell` adds `%USERPROFILE%\.local\bin` to your user PATH. Restart the
terminal, then verify:

```powershell
Get-Command porter        # want: Application, ...\.local\bin\porter.exe
porter --list
```

Note that plain `where` is a PowerShell alias for `Where-Object`; use `where.exe` or
`Get-Command`.

| | |
|---|---|
| `%USERPROFILE%\.local\bin\porter.exe` | the shim; this directory goes on PATH |
| `%APPDATA%\uv\data\tools\porter-serial\` | isolated venv holding pyserial |

**If `porter` opens the source in an editor instead of running**, `Get-Command porter`
will show `porter.py` rather than `porter.exe`. Two things are combining: `.PY` is in
your `PATHEXT`, and the source folder is on your PATH, so the bare word `porter`
matches `porter.py` first — and `.py` is associated with an editor, so it opens there.
Take the source folder off PATH; it should never have been on it:

```powershell
$p   = [Environment]::GetEnvironmentVariable('Path','User')
$new = ($p -split ';' | Where-Object { $_ -and $_ -ne 'C:\path\to\porter' }) -join ';'
[Environment]::SetEnvironmentVariable('Path', $new, 'User')
```

Read and write only the *User* PATH like that. `setx PATH "%PATH%;..."` expands the
combined system+user PATH into your user variable and truncates at 1024 characters,
which is the classic way to mangle it.

### Linux and macOS

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh    # if you do not have uv
cd /path/to/porter
uv tool install --editable .
uv tool update-shell
```

`update-shell` adds `~/.local/bin` to your shell profile. Start a new shell, then:

```sh
command -v porter && porter --list
```

The venv lands in `~/.local/share/uv/tools/porter-serial/`.

On Linux your user usually needs to be in the `dialout` group to open a serial port
(`uucp` on Arch). Log out and back in for it to take effect:

```sh
sudo usermod -aG dialout "$USER"
```

### Housekeeping

```sh
uv tool list                             # what is installed
uv tool dir --bin                        # where the shim went
uv tool install --editable . --force     # only if pyproject/dependencies changed
uv tool uninstall porter-serial          # note: package name, not command name
```

Code edits never need a reinstall. `pipx install --editable .` works the same way if
you already use pipx.

### Without packaging

Anything that puts the file on PATH is fine. On Windows, a `porter.cmd` in a directory
already on PATH:

```bat
@py -3 "C:\path\to\porter.py" %*
```

On Linux, `chmod +x porter.py` and symlink it as `porter` into `~/.local/bin`. You are
then responsible for `pyserial` being importable by whichever interpreter runs it,
which is the thing the uv route takes care of.

**Don't freeze it.** PyInstaller `--onefile` unpacks the whole bundle to `%TEMP%` on
every launch, costing a few hundred milliseconds, and reliably trips Defender /
SmartScreen on unsigned binaries. Startup is ~50ms plus one port enumeration.
Freezing is only worth it to hand the tool to someone with no Python at all, and then
`--onedir` beats `--onefile`.

## Why not just tio / SimpleCom / plink

They all connect you to *a* port you already named. porter's premise is that you don't
know the port number and don't want to care. Devices are tracked by **stable USB
identity** (`vid:pid:serial`), not by `COM14` / `/dev/ttyUSB0`, so the same board is
recognised across replugs even when Windows renumbers it.

## The picker

```
 porter                                                            2 devices

 > 1  codebot-3              COM14          239a:80f4   115200  +new
   2  jlink-uart             COM7           1366:1051   460800

 j/k or arrows select  .  enter connect  .  1-9 jump  .  b baud  .  a name
 h high contrast  .  esc log  .  q quit
```

- **The list is live.** A device that **just appeared** is tagged `+new` and the
  selection jumps to it — plug in the board, press enter. Arrivals and departures
  reach the picker as events; it never polls a snapshot of its own.
- Runs on the alternate screen buffer, so bouncing between picker and session leaves
  your device scrollback intact.
- `a` prompts for a name and saves it as an alias, keyed to the device's USB identity.
  Press `a` on an already-named device to rename it. The config path is shown at the
  bottom of the picker.
- **`esc` goes back to the console.** With a session behind it that means the session:
  the port is never closed while the picker is up, so resuming cannot reset your board,
  and device output that arrives meanwhile is buffered and flushed when you return.
- **With no session behind it, `esc` shows the console log** — the screen the device was
  talking to, still there after it disconnected. Scroll and select with your terminal's
  own scrollback; porter adds nothing to it but one line saying how to get back, which
  is erased on the way out. Only `esc` leaves, so `enter` is free to mean copy. `q` is
  the only way out of porter.
- **Losing a device always lands here**, with everything else still plugged in
  visible and selectable — and **the board you just lost is taken back on sight**:
  plug it in again while the picker is up and porter reconnects to it with no
  keystroke at all. That is the only thing that connects itself, and only on the
  replug: any *other* device that turns up is tagged `+new` and selected, so
  replug-then-enter is the whole reconnect. It is *one* attempt: if that open
  fails, the device is still listed and one enter away, but porter waits to be
  told rather than retrying by itself.
- **An open that has to retry can be given up on with `esc`.** A USB-serial device
  is often enumerated a moment before its driver will hand over the port, so porter
  retries while it settles rather than failing an open you would only press enter
  at again. That wait answers the keyboard like everything else does.
- `h` cycles the high-contrast themes, same key as in a session.
- Ports with no USB vid:pid (motherboard COM1/COM2, Bluetooth SPP, virtual sniffer
  bridges) are hidden. `--all` or `show_all = true` reveals them, and any port you have
  explicitly aliased is always shown — that is how you keep a real RS-232 port in the
  list.

## In-session keys

Borrowed from [tio](https://github.com/tio/tio), so muscle memory transfers both ways.
`d`, `n` and `h` are additions, on keys tio leaves unused.

| Key | Action | | Key | Action |
|---|---|---|---|---|
| `ctrl-t ?` | list commands | | `ctrl-t c` | show configuration |
| `ctrl-t q` | quit | | `ctrl-t L` | show line states |
| `ctrl-t d` | **back to device picker** | | `ctrl-t g` | toggle DTR/RTS |
| `ctrl-t n` | **next device** | | `ctrl-t b` | send break |
| `ctrl-t l` | clear screen | | `ctrl-t e` | toggle local echo |
| `ctrl-t h` | **high-contrast mode** | | `ctrl-t ctrl-t` | send a literal ctrl-t |

Everything else reaches the device untouched — including `ctrl-c`, which matters when
you are talking to a CircuitPython REPL.

## High contrast

Sunlight eats the default palette: dim greys vanish, and mid-tone colours stop being
distinguishable from each other or from the background. `h` — in the picker or as
`ctrl-t h` in a session — cycles

    default  ->  contrast-dark  ->  contrast-light  ->  default

`contrast-dark` is bright white on black, `contrast-light` is black on white. Pick
whichever wins against the glare you actually have; on most laptop panels that is the
light one outdoors and the dark one in the shade.

Both spend every distinction the default palette makes on legibility instead: no dim
text, no colour coding, just bold and reverse video for the selection and for anything
that needs to stand out. **Colour in the device's own output is flattened too** — an
ANSI-coloured REPL prompt would otherwise paint its own unreadable grey straight over
the top. Cursor movement and screen clears still pass through, so a full-screen program
on the far end keeps working; it just arrives monochrome.

The switch is sent to the terminal as OSC 10/11, so the scrollback already on screen
repaints as well — switch mid-session and the log you have been reading changes with
you, rather than leaving you with half a screen in the old theme. That works because a
high-contrast theme spends no colour of its own: a terminal keeps the colour a cell was
written with for ever, so anything porter stamped in would be frozen there. The screen
colours live in the terminal's defaults and nowhere else, and are handed back on exit.

**The selection highlight and the cursor come along with it** (OSC 17/19 and 12). They
have to: those colours come from your terminal's scheme, so `contrast-light` under a
dark scheme would otherwise paint a white background beneath a near-white selection
highlight, and a selection you cannot see is a log you cannot copy out of. The
selection is the theme inverted, and the cursor takes the foreground. A terminal that
does not implement those sequences ignores them and is no worse off than before.

Set `theme = contrast-dark` under `[porter]` to start that way — useful when you
already know you are heading outside.

## Config

tio-style INI at `~/.config/porter/config` (`%APPDATA%\porter\config` on Windows,
or `$PORTER_CONFIG`). Written with commented examples on first run.

```ini
[porter]
baudrate = 115200
show_all = false                   ; show ports with no USB id
exclude = COM1, *Bluetooth*, /dev/ttyS*
theme = default                    ; or contrast-dark / contrast-light

[codebot-3]
id = 239a:80f4:DF6202B3184E3033   ; one specific board
baudrate = 115200

[ftdi]
id = 0403:6001                     ; or any board of this type
baudrate = 921600
dtr = true
```

Baud precedence: picker `b` override > `--baud` > alias > `[porter]` > 115200.
Run `porter --list` to see the ids of what is currently plugged in.

## Windows Terminal

`.\install.ps1` puts porter in the new-tab dropdown for you. Restart Windows Terminal
and "porter (serial)" is there.

What it does, if you would rather do it yourself: Windows Terminal reads extra
profiles from **fragment extensions** — JSON files that third-party apps (Git Bash,
Anaconda, vendor toolchains) drop into a well-known folder. It picks them up at
startup; you never edit `settings.json`.

| For | Put the file in |
|---|---|
| just you | `%LOCALAPPDATA%\Microsoft\Windows Terminal\Fragments\porter\porter.json` |
| every user | `%ProgramFiles%\Microsoft\Windows Terminal\Fragments\porter\porter.json` |

Two things make this hard to find the first time:

- **None of those folders exist until something creates them.** Create the whole
  chain, including `Fragments\porter\`. The last component is an app name of your
  choosing and just keeps your file away from everyone else's.
- **It is not where `settings.json` lives.** That is
  `%LOCALAPPDATA%\Packages\Microsoft.WindowsTerminal_8wekyb3d8bbwe\LocalState\`,
  which has nothing to do with fragments. The `Fragments` tree sits directly under
  `%LOCALAPPDATA%\Microsoft\`, and holds for a Store-installed Windows Terminal too.

Copy `porter.fragment.json` there as `porter.json`, drop the `$help` key, and set
`commandline` to the full quoted path of `porter.exe` (`uv tool dir --bin` prints the
directory). A bare `porter` works too, but only once PATH has caught up — which it has
not if Windows Terminal was already running when you installed.

The profile's `icon` is `\uE88E`, the **USB** glyph from Segoe Fluent Icons — the
same entry Windows Terminal's own settings UI offers under *Built-in icon*. A
private-use codepoint there is rendered as a font glyph rather than read as a file
path, so it needs nothing installed alongside it.

## How it stays responsive

Every source porter reacts to runs on its own thread and posts to one queue. The
main loop's only wait is a `get()` on that queue.

| Source | How it waits |
|---|---|
| keyboard | native blocking wait — `WaitForSingleObject` on Windows, `select` elsewhere |
| device output | blocking read on the serial port |
| the port list | a timer in the watcher thread, which posts only when the set of ports *changes* |

Two properties fall out of that shape, and both are the point:

- **It cannot deadlock.** The queue is unbounded, so a post never blocks and no
  producer ever waits on the consumer; only the main loop waits on the queue, and
  nothing holds it while waiting. The wait-for graph is a star with every edge
  pointing at the queue, so no cycle is expressible.
- **No loop spins.** Nothing polls a snapshot. The one place that could still
  burn a core is a *failed* wait — a console handle that a sleep/wake cycle
  killed answers instantly instead of waiting — so a wait that could not wait
  sleeps a fixed floor before trying again, and re-arms the console on its way.

Device output is the exception to "everything is an event": it never goes on
the queue. A chatty port returns from `read()` thousands of times a second, so
the bytes are merged into one bounded buffer and only a *signal* is queued.
The loop does one terminal write per pass rather than one per read, and answers
keystrokes before painting — so `ctrl-t q` still works when a device has gone
berserk. If the device outruns the terminal for long enough the oldest bytes
are dropped, and porter says so on the line rather than leaving a silent gap.

The device list is the one thing on a timer rather than an OS notification. That
is deliberate: `WM_DEVICECHANGE` and netlink only say that *something* changed, so
finding out what still costs a full `comports()` walk. Native notification would
only lower the scan rate, at the price of three platform implementations and a
gap where macOS goes. The watcher scans fast while the picker is on screen and
slowly behind a live session, where nobody is reading the list and a lost device
is noticed by its reader faulting long before any scan.

Because that walk is pure-Python ctypes and holds the GIL, the watcher's sleep is
kept *proportional* to what the last scan cost. That is a cost bound, not a
staleness bound: an absolute ceiling on it would put a floor under the thread's
duty cycle as scans get slower, and a stale device list is a far smaller problem
than a UI that cannot be quit.

## Troubleshooting

`--debug` starts a watchdog. It writes a health line once a minute, and dumps
every thread's stack the moment the main loop misbehaves -- either stalled in a
native call or spinning. That is the right tool for both a freeze and a hot fan,
because neither leaves anything on screen:

    python porter.py --debug porter-debug.log

A line reads:

    14:22:01  5 ticks/s, port scan 41ms

`ticks/s` is how often the main loop came round. It waits on the event queue, so
this is a timer floor rather than a measure of load, and a number far above it
means something is generating events in a loop. `port scan` is what one
enumeration of the serial ports cost; on Windows that is a full device-tree walk,
and the watcher paces itself off it, so a slow one shows up as devices taking
longer to appear rather than as CPU.

## Tests

- `tests/t_unit.py` -- identity, alias matching, baud precedence, sort stability,
  theme cycling, the device-colour filter, key parsing, escape-sequence healing,
  the blocking wait's failure floor, suspend recovery, the event bus, the output
  hold, and port-watcher pacing and change detection.
- `tests/t_integ.py` -- drives the real program under a pty against socat-backed
  virtual serial ports: picker, data flow, every `ctrl-t` command, hotplug, loss
  landing in the picker, and a second device staying reachable while one is
  missing.
- `tests/t_stress.py` -- repeated unplug/replug cycles, orphan-thread leak check,
  and killing the pty out from under a live reader.
- `tests/t_ui.py` -- esc-resume with output buffering, reading the console log with
  no device connected, alias naming and renaming, non-USB filtering, high-contrast
  switching.

Needs `socat`.

    for t in unit integ stress ui; do python3 tests/t_$t.py || break; done
