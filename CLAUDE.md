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
- **No lock is ever held across a wait.** The main thread is the only writer
  to the terminal -- a reader posts and the loop paints -- which is what
  retired `_OUT_LOCK`, `Session._paused` and the reader's share of the SGR
  filter's state. The one lock left is `Bus._lock`, held only for a bytearray
  splice and never across `put()` or a write, so it cannot join the wait
  graph. A lock held while waiting is how the star grows a cycle.

`KEY` carries raw bytes, not tokens, because the session is a transparent pipe:
an arrow key has to reach the far-side REPL as the three bytes it arrived as.
Only the picker wants tokens, and it asks `keys()` for them.

### Device output never goes on the queue

`post_rx()` merges device bytes into one bounded buffer and queues only a
*signal*, at most one outstanding. This is not an optimisation; the version
that queued a chunk per read locked porter solid on Windows.

A chatty port returns from `read()` thousands of times a second. One queue
entry per read became one `write()`+`flush()` per read, ~3000 `WriteConsole`
round-trips a second saturated conhost until its writes blocked outright, and
because painting is on the main thread the keyboard went with it. Two rules
came out of that, and both must hold:

1. **One paint per pass, not one per read.** `take_rx()` returns everything
   that arrived, so the loop does a single write for a whole burst.
2. **Control events are answered before the paint.** A keystroke must never
   wait out a backlog -- that is the difference between `ctrl-t q` working and
   porter looking wedged. It costs strict ordering between porter's own lines
   and the device's, which is the right trade: the reorder window is one
   iteration and only opens when the device is already outrunning the
   terminal.
3. **A paint is never sooner than `PAINT_MIN` after the last one.** Rules 1
   and 2 are not enough on their own, and this is the subtle part: coalescing
   bounds how much output is *outstanding*, not how often porter *writes*. A
   terminal that keeps up is therefore the dangerous case, not the safe one --
   the loop free-runs, `take_rx()` hands back whatever landed since the last
   pass, and at 115200 baud that is about four bytes. Rule 1 still holds and
   porter is still back to one `write()`+`flush()` per read.

   That is not hypothetical: it is how the bug came back. A board replugged
   mid-session put the main loop at 3123 passes/sec, and then `flush()` simply
   stopped returning -- pinned there for over a minute, with the reader,
   watcher and key-reader threads all healthy and a keyboard that was dead
   only because nobody was left to drain the bus. Deferring keeps the chunks
   fat: same bytes, ~47x fewer console round-trips, one frame of latency.

   While a paint is deferred the bus goes quiet by itself -- the RX signal is
   still outstanding, so the reader queues nothing and just keeps merging --
   and the loop shortens its own `BUS.get()` timeout to wake for it. Covered
   by "a flood is painted at a bounded rate" in `t_unit.py`, whose producer
   `sleep()`s rather than spins: a producer that holds the GIL starves the
   main loop and hides this bug completely.

A loop that *owns* the screen -- the picker, a page -- has nothing to paint,
and claims the signal on its own timeout rather than the moment it arrives.
Held output never reaches the console so it cannot saturate anything, but
`take_rx()` re-arms the signal, the reader posts again at once, and the loop
free-runs at the reader's rate for no benefit whatever. Caught at 316
passes/sec with a page open over a chatty board -- harmless, and still past
`SPIN`, which exists to say no loop has a reason to be there. `_claim_rx()` is
the one place that knows this, and every exit from those loops goes through it
in a `finally`: a signal left outstanding is never re-armed, which is the
live-port-dead-screen bug again, and the picker alone has half a dozen ways
out.

`RX_MAX` bounds what has arrived but is not yet painted, because a device can
outrun any terminal indefinitely and the queue is unbounded by design. Past it
the oldest bytes go -- and the count is kept and reported by `note()`, because
a gap in the log that says why it is there is worth far more than a silent
one. Covered by "a chatty device against a slow terminal" in `t_stress.py`,
which fails loudly against the version that queued per read.

Painting is still on the main thread, so a terminal that stops accepting
output entirely -- a Windows console with a selection active, for instance --
still blocks porter, quit included. Rule 3 removes porter's own ability to
*cause* that by saturation, which is what the replug lockup turned out to be,
but it does not cover a console wedged from the outside. If that turns out to
matter, the fix is a painter thread owning stdout, not a lock around
`w_bytes`.

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

### A port with no name is not a port, and the diff is on identity *and* name

Two rules, from one bug: porter announcing `could not open port ''` over and
over, and needing a physical replug to recover.

**`_named()` drops entries whose `device` is empty, at the source.** Windows
enumerates a USB-serial device the moment the class driver claims it, but the
COM number is a registry value the driver writes slightly later -- and
pyserial's Windows backend reads that value without checking whether the read
succeeded, so a device caught mid-attach comes back *fully described* with
`device` set to `''`. It is not a port; nothing downstream should ever see it.
The filter is in `PortWatcher.run` and in `_scan_ports`, and it must stay at
that boundary rather than at each use, because the damage is not just a failed
open:

- `_identity()` is port-independent, so the nameless entry carries **the same
  key as the real entry about to appear**. Left in the list it is an arrival
  for the very device the picker is waiting for, and porter opens `''`.
- Worse, that key is now *present*. When the real name lands, the key set has
  not changed, so on the old key-only diff **nothing was posted at all** -- a
  list stuck pointing at a port that never existed, where enter just repeats
  the failure. Unplugging is the only thing that clears it, which is exactly
  what the user had to do.

**The watcher's diff and the picker's are on `(identity, port)`.** They answer
different questions and both are needed: "what just arrived?" is identity
alone -- a board back on a different COM number is the same device, not a new
one -- but "is what is on screen still true?" has to include the name, or a
renamed port is invisible. `_listing()` is the picker's half of that.

Covered by "a port with no name is not a port" in `t_unit.py`, whose last two
checks are the two halves of the bug: the nameless entry must not be
published, and the real name landing must still post.

### An automatic retry must be interruptible, and must be bounded

`_open_port` retries while the driver settles, and that wait is on the bus
(`_wait_bus`), not a `sleep`. It has to be: this runs on the main thread with
no session and no picker behind it, so it is the one stretch of porter that
has nothing on screen and nothing to paint. A sleep there does not merely
ignore the keyboard for three seconds -- the keys are not lost, they queue and
arrive afterwards in the picker, where enter starts the same open again. The
report was "porter is stuck", and that is what it was. `esc` and `ctrl-c` give
up; every other key is **discarded**, because with no port open there is
nowhere for a byte to go and queueing it is what closed that loop.

It leaves the RX signal outstanding (a reader leaked from an earlier session
may still be posting -- see `_claim_rx()`) and it always costs its time, or a
caller that loops on it is a spin. Giving up is reported as giving up, not as
a failure: `Session.open()` owns the whole line it returns, `CANCELLED` and
all, so `main` only has to `note()` it.

Covered by "the wait between open attempts is on the bus" in `t_unit.py`.

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

### A background thread may not die, and may not die quietly

`KeyReader` and `PortWatcher` catch everything their loop body can raise, and
neither is allowed to fall out of `run()`. This is not general defensiveness:
these two threads each own a whole faculty, and losing one is
indistinguishable from a lockup by the only test that matters -- what the user
sees. A dead `KeyReader` is a keyboard that stopped working. A dead
`PortWatcher` is a device list frozen for the rest of the run, in a picker
whose entire job is to be live. Neither leaves a mark on screen.

The watcher's guard has to cover **the diff, not just the scan**.
`comports()` was already wrapped; `_identity()` was not, and it reads
attributes off whatever the platform backend happened to build.

What they catch goes in `FAULTS`, and the main loop drains it -- `note()` in a
session, the message line in the picker, which owns its own display. Two
things follow from where that lives:

- **It is a deque, not a bus event.** A fault must not be lost, and an event
  consumed by a modal page would be. `maxlen` makes append and popleft atomic,
  so it needs no lock and cannot join the wait graph.
- **A recovering thread still has to cost time.** `KeyReader` sleeps
  `DEAD_WAIT` after a fault. A caught exception that costs nothing is the same
  pegged core as a failed wait that returns instantly -- see above; it is the
  same rule, and catching rather than crashing does not exempt you from it.

## Losing a device lands in the picker, and only that device comes back

**Losing a device always lands in the picker.** This is the important property,
and it is not just cosmetic. An earlier version waited in place watching only
the device it had lost, which meant anything *else* plugged in during the wait
was invisible and porter looked wedged with no way out but a keystroke. The
picker is already a live device monitor; putting the wait anywhere else
re-creates that dead end. Covered by "a second device stays reachable while one
is missing" in `t_integ.py`.

**The device that was lost is the one thing porter reconnects to on its own.**
`picker(awaiting=...)` takes the key of the device whose session ended, and
returns that device the moment it *appears* -- so unplug, replug, and you are
back on it with no keystroke. Everything else that turns up is tagged `+new`
and the selection jumps to it, and still has to be chosen.

That this is one variable and one condition is the whole point. The standalone
auto-reconnect that was removed drove the connection from *outside* the picker
and had earned a `SETTLE` window, a `FLAP_FLOOR`, a re-arm that had to survive
a failed open, and a rule about matching the appearance edge rather than
presence -- four pieces of state on the one loop porter can drive with no human
in it. **Do not bring that back.** The picker is already watching the port set,
so what lives there instead needs none of it:

- **The appearance edge, not presence**, and only within one visit to the
  picker: a device already in the list when the picker opened is not an
  arrival. That is what keeps a failed open from becoming a retry loop -- the
  picker reopens with the device present, nothing appears, and it waits to be
  picked like anything else. It is also why there is no `SETTLE` and no
  `FLAP_FLOOR`: the watcher's diff is already the debounce.
- **One attempt, then it is the user's turn.** `awaiting` is cleared when the
  open it caused fails, so a device that *flaps* in the enumeration cannot
  turn the appearance edge into a retry loop -- every flap is another arrival,
  and an arrival porter connects to by itself is a loop with nobody driving
  it. The watcher's diff debounces a real device, not an artifact of
  enumeration (see `_named()`), which is why presence-vs-edge is not enough on
  its own. Still one variable and no timers, and the device is one keystroke
  away in the picker porter lands in.
- **Only the device that was lost.** `awaiting` is set when a session ends in
  `LOST`, and in the picker itself when `GONE` arrives for the session behind
  it -- the unplug-while-the-picker-is-up case, which never reaches `main`
  until `esc` is pressed. It is cleared the instant any session opens, so
  connecting to something else ends the wait.
- **Only in the picker.** Nothing watches for a lost device from a live
  session, because there is no live session to watch from.

`Session.lost` is the other remnant -- it lets a session that died while the
picker was up report the disconnect rather than resume, so `esc` still means
"never mind" and never silently becomes quit.

Covered by "the picker takes back the device whose session was lost" in
`t_unit.py` (including the three cases that must *not* fire) and by the
unplug/replug cycles in `t_stress.py`.

### `GONE` names the session, not the device

`close()` says outright that it can leave a reader behind. A leaked reader
faults whenever its driver gets round to it -- long after the session that
owned it is over -- and it posts `GONE` when it does. Keyed by device that is
indistinguishable from the *live* session's own reader, because a replug of
the same board carries the same key, which is exactly the case porter is built
around. So the payload is the `Session` object and both consumers compare
identity: a session ends only on its own reader's fault.

### A port the last reader still holds says so

The reader owns closing its own handle, so one that would not stop keeps the
port open, and every reopen fails with a bare "access is denied" that reads
like a broken device. It is not -- it is porter still holding it. `close()`
records the thread in `_LEAKED`, `_holder()` forgets it once it lets go, and
the open failure names it. Same rule as the dropped-byte count: a failure that
says why is worth far more than one that does not.


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
