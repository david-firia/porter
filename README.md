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

Install uv if you do not have it, then open a new terminal:

```powershell
winget install --id=astral-sh.uv -e
```

Install porter from the folder holding `porter.py` and `pyproject.toml`:

```powershell
cd C:\path\to\porter
uv tool install --editable .
uv tool update-shell
```

`update-shell` adds `%USERPROFILE%\.local\bin` to your user PATH. **Restart the
terminal** — PATH is read at process start — then verify:

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

 j/k or arrows select  .  enter connect  .  1-9 jump  .  b baud  .  a alias  .  q quit
```

- Polls every 400ms. A device that **just appeared** is tagged `+new` and the selection
  jumps to it — plug in the board, press enter.
- Runs on the alternate screen buffer, so bouncing between picker and session leaves
  your device scrollback intact.
- `a` prompts for a name and saves it as an alias, keyed to the device's USB identity.
  Press `a` on an already-named device to rename it. The config path is shown at the
  bottom of the picker.
- **`esc` goes back to the session you came from** — the port is never closed while the
  picker is up, so resuming cannot reset your board, and device output that arrives
  meanwhile is buffered and flushed when you return. With no session behind it, `esc`
  exits. `q` always exits.
- Ports with no USB vid:pid (motherboard COM1/COM2, Bluetooth SPP, virtual sniffer
  bridges) are hidden. `--all` or `show_all = true` reveals them, and any port you have
  explicitly aliased is always shown — that is how you keep a real RS-232 port in the
  list.

## In-session keys

Borrowed from [tio](https://github.com/tio/tio), so muscle memory transfers both ways.
`d` and `n` are additions — tio leaves them unused.

| Key | Action | | Key | Action |
|---|---|---|---|---|
| `ctrl-t ?` | list commands | | `ctrl-t c` | show configuration |
| `ctrl-t q` | quit | | `ctrl-t L` | show line states |
| `ctrl-t d` | **back to device picker** | | `ctrl-t g` | toggle DTR/RTS |
| `ctrl-t n` | **next device** | | `ctrl-t b` | send break |
| `ctrl-t l` | clear screen | | `ctrl-t e` | toggle local echo |
| `ctrl-t ctrl-t` | send a literal ctrl-t | | | |

Everything else reaches the device untouched — including `ctrl-c`, which matters when
you are talking to a CircuitPython REPL.

## Config

tio-style INI at `~/.config/porter/config` (`%APPDATA%\porter\config` on Windows,
or `$PORTER_CONFIG`). Written with commented examples on first run.

```ini
[porter]
baudrate = 115200
show_all = false                   ; show ports with no USB id
exclude = COM1, *Bluetooth*, /dev/ttyS*

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

`porter.fragment.json` registers porter as a profile in the dropdown. Fix the path,
then drop it at `%LOCALAPPDATA%\Microsoft\Windows Terminal\Fragments\porter\porter.json`.

## Troubleshooting

`--debug` starts a watchdog that dumps every thread's stack to a log if the main
loop ever stops ticking. That is the right tool for a freeze, because a blocked
main thread has nothing left to report with:

    python porter.py --debug porter-debug.log

## Tests

- `tests/t_unit.py` -- identity, alias matching, baud precedence, sort stability.
- `tests/t_integ.py` -- drives the real program under a pty against socat-backed
  virtual serial ports: picker, data flow, every `ctrl-t` command, hotplug,
  auto-reconnect.
- `tests/t_stress.py` -- repeated unplug/replug cycles, orphan-thread leak check,
  and killing the pty out from under a live reader.
- `tests/t_ui.py` -- esc-resume with output buffering, alias naming and renaming,
  non-USB filtering.

Needs `socat`.

    for t in unit integ stress ui; do python3 tests/t_$t.py || break; done
