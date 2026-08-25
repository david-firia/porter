# porter

A serial terminal with a live device picker. Single file: `porter.py`.

## Key bindings must stay compatible with tio

porter deliberately borrows its in-session key map from
[tio](https://github.com/tio/tio) so that skill transfers in both directions:
someone who knows tio can drive porter, and vice versa. That only holds if we
keep two rules when adding a command.

**Rule 1 — never rebind a key tio already defines.** If tio uses a key, porter
either implements the same meaning under it or leaves it alone. A key that means
one thing in tio and something else in porter is worse than no key at all,
because it fails silently and destructively (`ctrl-t x` sending a file in one
tool and, say, exiting in the other).

**Rule 2 — never duplicate a tio feature under a different key.** If we grow a
feature tio already has, it goes on tio's key. Adding "show statistics" under
anything but `s` splits the muscle memory even though nothing is overridden.

New features that tio has no equivalent for go on keys tio leaves unused, and
get marked as porter additions in the README table.

### tio's reserved key commands (prefix `ctrl-t`)

Verified against tio's `man/tio.1.in` and `src/tty.c`. **Do not assign any of
these to a different meaning.**

| Key | tio's meaning | | Key | tio's meaning |
|---|---|---|---|---|
| `?` | list available key commands | | `p` | pulse serial port line |
| `b` | send serial break | | `q` | quit |
| `c` | show configuration | | `r` | run script |
| `e` | toggle local echo mode | | `R` | execute shell command against device |
| `f` | toggle log to file | | `s` | show TX/RX statistics |
| `F` | flush data I/O buffers | | `t` | toggle line timestamp mode |
| `g` | toggle serial port line | | `v` | show version |
| `i` | toggle input mode | | `x` | send file via XMODEM |
| `l` | clear screen | | `y` | send file via YMODEM |
| `L` | show line states | | `ctrl-t` | send a literal ctrl-t |
| `m` | change input/output character mapping | | | |
| `o` | toggle output mode | | | |

Note `t` is **timestamps**, not "send ctrl-t" — that is `ctrl-t ctrl-t`. It is an
easy one to get wrong from memory.

### Free for porter

Everything tio does not bind. Currently unused and available: `a` `d` `h` `j`
`k` `n` `u` `w` `z`, every uppercase letter except `F` `L` `R`, and the digits.

`h` is worth a caution: tio puts help on `?`, but `h` is the obvious second
choice if tio ever adds one. We use it for the theme toggle. If upstream tio
claims `h`, that is a conflict to resolve, not to ignore.

### porter's current map

Same-as-tio: `?` `q` `e` `l` `b` `c` `L` `g` `ctrl-t`.
porter additions on free keys: `d` (back to picker), `n` (next device),
`h` (cycle high-contrast themes).

Anything not recognised is passed through to the device as `ctrl-t` + the byte,
so an unbound key is inert rather than swallowed.

### The picker is a separate namespace

Keys in the device picker (`j`/`k`, `b`, `a`, `r`, `h`, digits, …) are pressed
bare, with no `ctrl-t` prefix, and tio has no picker. They do not have to match
tio and are not constrained by the rules above — but keep a key that exists in
both modes meaning the same thing in both (`h`, `b`, `q`).

## Where a message goes

porter's own output must not silently pollute a capture of the device's
output -- but it must not silently *omit* anything either. A log with an
unexplained gap in it is worth less than one that says why the gap is there.
So the test is not "is this porter talking?" but "does this belong in the
record?"

Three destinations, in `porter.py`:

| | Use | Reaches the scrollback |
|---|---|---|
| `note(text)` | porter changed the wire, or the connection changed | yes |
| `_page(body)` | porter is answering a question about itself | no |
| `toast(text)` | a one-line ack for a key just pressed | no |

**`note()` — in the record.** Connect, disconnect, reconnect and the buffered
byte count, `cannot open`, `break sent`, DTR/RTS actually changing, and the
`L` line-state reading. When you are reading back a boot log and the data
stops, `[porter] codebot-3 disconnected` is the most valuable line on the
screen. Never move these off the stream to make it "cleaner".

**`_page()` — modal, on the alternate screen.** Help, configuration, and the
`g` line-toggle prompt. Device output is held while a page is up and flushed
into the main buffer on the way out, so the page costs the log nothing. A
page is modal like the picker is: keys typed while it is up belong to the
page and do not reach the device. Backstopped at `PAGE_SECS` so walking away
from an open page cannot quietly overrun `BACKLOG_MAX` and drop output.

**`toast()` — transient, over the session.** Theme and local-echo acks. One
slot, so repeated presses replace rather than stack; `TOAST_SECS` or any key,
whichever comes first.

Two invariants a toast must not break:

1. **The dismissing key still reaches the device.** Dismissal is a side
   effect of the keystroke, never a consumer of it -- otherwise porter stops
   being a transparent pipe and starts eating REPL input. `Session.run()`
   clears the toast *before* the byte is echoed or forwarded. Covered by
   "key that dismisses an ack still reaches the device" in `t_integ.py`.
2. **The device is held while a toast is up.** Anything painted into the main
   buffer is subject to scrolling, and a toast that scrolls leaves a fragment
   in the scrollback -- the exact pollution this exists to avoid. The hold is
   what makes the erase-to-end-of-line exact.

The hold is `Screen`, not the reader: the main loop never stops draining the
bus, it just parks device bytes while porter owns the screen. Holds nest, so a
page opening over a held session releases back to held, not to the device. That
is also what lets the queue stay unbounded -- the cap lives in one place, owned
by the one thread that touches it.

The hold is the cost: device output is delayed by up to `TOAST_SECS` after a
command key. If that ever reads as laggy against a chatty device, the upgrade
is a one-row DECSTBM scroll region installed only while the toast is up, so
output keeps flowing above it. Same call sites, different paint function --
do not reach for it before the delay is actually a problem.

## Everything is an event, and the main loop only ever waits on the queue

Every source porter reacts to runs on its own thread and *posts* to one
`Bus`. The main loop's only wait is `BUS.get()`. Keep it that way -- the
deadlock-freedom is structural, not the result of having checked:

- **The queue is unbounded**, so a post never blocks and no producer can end up
  waiting on the consumer. A capacity here would put every producer back into
  the wait graph. Held output is bounded by `Screen` instead, where the main
  thread owns it.
- **Only the main loop waits on the queue**, and nothing holds it while
  waiting. The wait-for graph is a star with every edge pointing at the queue,
  so no cycle is expressible.
- **No locks.** The main thread is the only writer to the terminal -- a reader
  posts `RX` and the loop paints it -- which is what retired `_OUT_LOCK`,
  `Session._paused` and the reader's share of the SGR filter's state. Adding a
  lock back is a sign something is being done on the wrong thread.

`KEY` carries raw bytes, not tokens, because the session is a transparent pipe:
an arrow key has to reach the far-side REPL as the three bytes it arrived as.
Only the picker wants tokens, and it asks `keys()` for them.

### A failed wait must still cost time

This is the one rule that survives going event-driven, because going
event-driven does not save you from it. A console handle that a sleep/wake
cycle killed does not block -- it fails *immediately* -- and a wait that returns
instantly for ever is the same pegged core a poll was, only written more
elegantly. `_wait_input` sleeps `DEAD_WAIT` on a failed wait and re-arms the
console. Any new blocking wait needs the same floor.

The sibling rule is that a wait must *consume* what it woke for. `_read_raw`
reads the console records even when they hold no key, which leaves the handle
unsignalled so the next wait blocks. That is what retired the old drain pacing;
a wait that peeks without consuming is a spin.

### Never put an absolute ceiling on the watcher's sleep

`PortWatcher` sleeps `max(interval, last_scan * SHARE)`. That is deliberately
**proportional**, and it must stay that way. `list_ports.comports()` is a
pure-Python ctypes walk that holds the GIL for most of its duration, so the
sleep is not really about staleness -- it is the only thing keeping this thread
to a fixed share of one core.

An absolute cap on that sleep reads like it bounds how stale the device list
can get. What it actually does is put a *floor* under the thread's duty cycle
as scans get slower: a 10s enumeration under a 2s cap is 83% of a core with the
GIL held, the main thread stops getting scheduled, and porter locks up with a
dead keyboard and the fans running. This has been tried; it does not work. A
stale device list is a far smaller problem than a UI that cannot be quit.

### The device list is on a timer on purpose

`WM_DEVICECHANGE` and netlink only say that *something* changed -- finding out
what still means a full `comports()` walk, so the expensive part is identical
and all a native source buys is a lower scan rate. One timer is one
implementation instead of three plus a gap where macOS goes. If the latency
ever genuinely matters, a native source drops in as one more producer posting
`PORTS` to the same bus, and nothing else moves.

What makes it event-driven is the **diff**: the tick is private to the watcher,
and only a change in the port set leaves the thread. Nothing downstream polls a
snapshot. `attention()` scans fast while the picker is on screen and slowly
behind a live session, where nobody is reading the list and a lost device is
noticed by its reader faulting long before any scan would.

### An empty read must always cost its timeout

`Session._read_loop` sleeps out the remainder of `ser.timeout` whenever a read
comes back empty. A handle whose device has gone can return empty immediately
rather than blocking, and without that sleep the loop is a spin that holds the
GIL. It compounds badly: `close()` leaks a reader it cannot join within 2s, so
one dead port can starve the main thread for the rest of the run.

A thread leaked *blocked* holds no lock and no GIL and is survivable. A thread
leaked *spinning* is not. That asymmetry is why the rule exists.

### Do not name a thread's stop flag `_stop`

`threading.Thread._stop` is a real method that `join()` calls once the thread
has finished. An `Event` attribute of that name shadows it and `join()` raises
`TypeError` instead of joining. `PortWatcher` and `KeyReader` use `_quit`.

## Losing a device lands in the picker, and nothing reconnects by itself

**Losing a device always lands in the picker.** This is the important property,
and it is not just cosmetic. An earlier version waited in place watching only
the device it had lost, which meant anything *else* plugged in during the wait
was invisible and porter looked wedged with no way out but a keystroke. The
picker is already a live device monitor; putting the wait anywhere else
re-creates that dead end. Covered by "a second device stays reachable while one
is missing" in `t_integ.py`.

Auto-reconnect was removed deliberately and is **deferred, not forgotten**. It
had earned a `SETTLE` window, a `FLAP_FLOOR`, a re-arm that had to survive a
failed open, and a rule about matching the appearance edge rather than presence
-- four pieces of state on the one loop porter can drive with no human in it.
Do not reintroduce it until the event-driven core has had real running time.

What replaces it costs nothing: a device that appears in the picker is tagged
`+new` and the selection jumps to it, so replug-then-enter is the whole
reconnect. `Session.lost` is the only remnant -- it lets a session that died
while the picker was up report the disconnect rather than resume, so `esc`
still means "never mind" and never silently becomes quit.


## When adding a command

1. Check tio's reserved keys before picking a letter.
2. Pick its destination from the three above before writing it.
3. If it waits for anything, wait on the bus -- never on the terminal directly,
   and never while holding the screen without releasing it in a `finally`.
   `_page()` is the worked example.
4. Update all six places a binding lives: `HELP` and `_command()` in
   `porter.py` for session keys, the picker footer and picker dispatch in
   `picker()` for picker keys, the README key table, `tests/t_ui.py`, and the
   starter config text if it mentions the key.
5. Re-verify the table against upstream tio if it has been a while — tio adds
   commands between releases.
