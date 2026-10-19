# The status line and what the model sees

[README](../README.md) · [All documentation](README.md)

## What the model sees

With every prompt:

```
Usage: 5h 40 % (resets 14:00) · week 61 % (resets Mon 19 Jan 09:00) · measured 0 min ago · Now: Wed 14 Jan 2026 10:05
```

At or above `start_block_5h` the line says so (`— from 75 % on, no new blocks`). At or above `start_block_week` it
adds a weekly note that follows `thresholds.week_action`: `week at or above 75 %: agents start with a warning`
(`warn`), `…: new agents need the user's approval` (`ask`) or `…: no new agents without --week-ok-until` (`deny`).
Both notes can stand together. With a missing or old measurement it says `unknown` or `STALE` and
asks to start larger blocks only after a fresh measurement. Without a weekly value it shows `week ?`, and a
measurement stamped slightly ahead of your clock shows `measured in the future`. The texts are English; the
model relays them to you in your language.

The time part is useful on its own: every message in the transcript carries its date and time, so you can
find later when something happened. Turn either part off in `config.json` (`display.show_usage`,
`display.show_time`).

### The context window

```
Context: 168k/1000k tokens (17%). Measured, not estimated.
```

The context reporter adds this line to every prompt, and after a tool call once the context has reached
`context_reporter.after_tool_from` (default 50 %) of the window, so a long run is reminded as the context gets
expensive and not after every single call. The numbers are those of the `ctx` part of the status line, with the
tokens cut to thousands and the percentage rounded the same way. Without them the model can only guess how full
its context is.

It reads nothing but the per-session file the status line writes (see below), never the transcript. It stays
silent, and says nothing at all, when:

- the status line is not installed, or has not measured this session yet (before the first answer),
- the measurement is older than `context_reporter.max_age_min` (default 15 minutes) or from the future,
- the file is missing, broken, a link, or belongs to another session,
- the call comes from a subagent: its context is another one, and the file describes the main session.

Several sessions at once each get their own numbers. The texts are English; the model relays them to you in
your language.

### The wake-up watcher

A model does nothing while you are away, but the prompt cache stays warm for up to an hour. If you leave a long
session without saying goodbye, the cache goes cold and the next message writes the whole context again. The
wake-up watcher uses the warm cache: shortly before it expires it wakes the model, which then runs its
session-end routine (save progress, commit, push, summary) while the context is still cheap to read.

It is off by default. Switch it on by setting `wake_watcher.from_k` to the context size (in thousands of tokens)
from which you want it, for example `200`. From then on the context reporter adds one sentence to the prompt line:
the model starts `wake_watcher.py` once in the background (it is copied by the installer, no settings entry). The
watcher checks once a minute and ends with a message, which Claude Code hands to the model as the result of the
background task:

- It fires when the context has reached `from_k`, the cache is warm, and the cache expires within the lead time:
  `wake_watcher.lead_min` (default 10 minutes), but at most a fifth of the cache lifetime, so 10 minutes for the
  1-hour cache and 1 minute for the 5-minute cache. The message says how long you were away, how much cache is left
  and the context size, and tells the model to run its session-end routine without asking and not to start another
  watcher.
- A cold or unknown cache never fires: waking a cold cache would write the whole context again, which is what it
  is meant to avoid. When you write, the expiry moves and the watcher waits on.
- It stops with a short message that asks for nothing (Claude Code still delivers it, after `/clear` to the new
  session) when the session is gone (after `/clear` or closing it: no fresh state file on
  three checks in a row; a sleeping computer does not count), after 115 minutes (Claude Code ends background tasks
  after two hours; the turn that delivers its message already offers the next one), when another watcher for the
  session lives, or when `from_k` is 0.
- After 115 minutes it does not simply restart if you have been away for a long time: that restart message is itself
  a model turn, which would renew the cache and start the idle time again while you are gone. So when the watcher
  reaches its time limit with a warm cache, a context of at least `from_k`, and at least 20 minutes of idle time
  (the cache lifetime minus what is left), it sends the wake-up call instead. With the 5-minute cache the idle time
  never gets that long, so it restarts as before.
- After a wake-up the offer rests for an hour, so the routine does not start the next watcher.

What you need for this:

- The status line, with its `refreshInterval` (the installer sets 60 seconds). The watcher reads the cache clock
  from the state file the status line writes; without refreshing it would see an idle session as gone.
- A **session-end routine**: what "save progress, commit, push, summary" means for your projects. The wake-up
  message alone is the output of a tool, and a model rightly does not follow instructions from tool output on its
  own. Two ways to close that gap:
  - Put the routine in a file and set `wake_watcher.routine_file` to its path. When the watcher fires, Claude Code
    submits its end as a notification, which runs the context reporter; it sees that the task's output is this
    session's wake-up call and adds your file to the context, as hook context like the context line. The file must
    be a regular file of yours that group and others cannot write, UTF-8, at most 4 KB; otherwise nothing is added
    and the log gets one line. [`examples/wake-routine.md`](../examples/wake-routine.md) is a start; the installer
    does not copy it, so an update never touches your file.
  - Or write it into the instructions the model reads (for example your `CLAUDE.md`), with one line that names the
    wake-up call as a trigger for the routine.
- Only the main session is offered the watcher, never a subagent, and only with a prompt, never after a tool call.

## What the status line shows

```
Opus | 5h 40% ↻14:00 | week 61% ↻Mon 09:00 | ctx 231k/1000k 23% | cache 54m
```

![The status line with the five-hour window in red at 82 %](images/statusline-red.png)

Later the same evening: the five-hour window has passed 75 % and turns red. With the default thresholds the
spawn gate now starts no new agent blocks until the window resets at 22:40.

- `5h` and `week` come from `rate_limits` (yellow from 50 %, red from 75 %); at the weekly threshold
  (`start_block_week`) a marker names what the spawn gate now does, by `thresholds.week_action`:
  `<- week gate: agents warn`, `<- week gate: agents ask` or `<- week gate: agents blocked`. The weekly limit itself
  is still ahead then.
- `↻14:00` is when the window resets, in your local time. The weekly one carries the weekday (`↻Mon 09:00`), except
  when it resets later today, and appears only from yellow on; below that it is days away. A missing or past reset
  time is left out (`statusline.show_reset`).
- `ctx` is the input context of the session: tokens used, window size, share of the window. It is shown from
  the first prompt on, as soon as Claude Code reports the window size; before the first answer it reads `ctx –/1000k` instead of a made-up zero. The colour
  depends on the absolute tokens, not on the percentage: yellow from 300k, red from 500k
  (`statusline.context_yellow_from_k`, `context_red_from_k`), because a long context costs the same on a small
  and on a large window.
- `cache` is how long the prompt cache of the session stays warm: minutes left, yellow below 5
  (`statusline.cache_yellow_below_min`), red `cache cold` when it has run out or is not warm. Nothing is shown
  until the first request.
- A part whose input is missing is left out, never filled with a guess.

### In the Claude desktop app

The desktop app does not show the `statusLine`. A plugin in [`desktop/`](../desktop/README.md) draws the same line
under its prompt, with the thresholds of your `config.json`:

```
claude plugin marketplace add inoX-Network/claude-code-usage-clock
claude plugin install usage-clock-display@usage-clock
```

Two differences: the app tells a plugin no cache expiry, so the cache clock is an estimate from the last answer
(`cache ~54m`), and the cost of the session follows at the end. In the terminal the plugin draws nothing.

The plugin also writes the app's numbers into the usage file, through the installed status line, so the hooks and
gates work in a desktop session as in the terminal ([details](../desktop/README.md#it-writes-the-usage-file-too)).

It adds `/report` as well, in the desktop app and in the terminal: a pane with the usage report
([The /report pane](reports.md#the-report-pane)). In the terminal that is all the plugin does, so install it there too if you
want the pane.

### Files the status line writes

While a context is measured, the status line also writes `<log_dir>/context/<session_id>.json` (window size,
used tokens, time, and while the prompt cache is warm its lifetime and expiry) for each session, private to you
(mode 0600) and cleaned up after 7 days. The context reporter and the wake-up watcher read that file; without the
status line there is nothing for them to read.

It also keeps a **measurement history** of the usage numbers, `<log_dir>/usage-history.jsonl`, for reports over
days, weeks and months. One line per measurement, numbers only (no session, no path):
`{"t":"2026-01-14T10:05:00Z","five":40,"week":61,"five_resets":1768399200,"week_resets":1768813200}`. A line is
added when `five`, `week` or a reset time changed since the last line, or when the last line is
`history.heartbeat_min` old (15 minutes), so an idle stretch shows up as a stretch and not as a gap. The numbers are
the merged ones that all your sessions share, written under the lock of the usage file, so parallel sessions never
add the same measurement twice. Above `history.max_kb` (1024 KB, about 40 days) the file becomes
`usage-history.jsonl.1` and an older `.1` goes. The file is private to you (mode 0600). `history.enabled: false`
switches it off.
