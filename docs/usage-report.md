# The usage report

The details of `tools/usage_report.py`. What it does, its commands and an example of the overview are in the
[README](../README.md#reports-what-was-used); every line and JSON field is explained in
[`report-format.md`](report-format.md).

## The overview

What the parts of the overview mean; the example is in the [README](../README.md#the-overview).

- **Where it went:** the projects by cost at API prices (the **cost weight**, see below), the five largest as rows and
  the rest as one line. A project is the name of the folder a session ran in, the same for everyone and without a
  setting: the report shows where you worked. It knows no paths, so folders of the same name in different places are one
  project. A name longer than 30 characters loses its middle for a `…` in the table (`my-company-fron…dmin-dashboard`); the
  JSON, `--full` and the `Details:` line always have the full name, and two projects that would look the same after
  shortening are both shown whole. `~week pts` is the estimated share of the weekly limit, in points (1 point
  = 1 percentage point of the week); `<1` is less than one point.
- **Start:** the size of the first request of a session or subagent in tokens, measured: the median over the main
  sessions, and the lowest and highest median among the subagent types (see **Startup context** below). The line is
  left out if the period has no start.
- **Notable:** a line appears only if one of these fixed rules hits, and then for everything it hits, however small
  the project: a gate denied or asked about something (with the reasons, see below), a 5h window reached
  `thresholds.start_block_5h` (default 75 %), a project's cache hit rate is below 90 %, one project made more than half
  of the cost, or the subscription rose with no request with a price in between ("unexplained"). Without a hit the
  block is left out.
- **Details:** `--project NAME` (the full name of the project, or the beginning of it) and `--session ID` (the
  beginning of a session id). Either must be unique, else the report lists the candidates and stops with exit code 2.
  They print the blocks that
  `--full` has for those sessions, and their sum. They combine with every period and with each other. `--json` has
  all projects and sessions in one object and takes none of the three.

## The full report

Days are local days. `--week` is the subscription week, not the calendar week: it ends at the weekly reset time
that the measurement history last saw, else the one in the shared usage file (`seven_day.resets_at`), and counts on in
steps of seven days. With neither it stops with exit code 1 and asks for `--since`.
`--since` and `--until` are whole days, both included. The log folder is found like `log_summary.py` finds it
(`--config PATH`, else `CCUC_LOG_DIR`, else the config file of your installation). Exit code 0 is a report (an empty
transcripts folder gives an empty one), 1 the transcripts folder is missing (or a price file that cannot be read, or an
archive that cannot be written), 2 bad arguments, also a day outside the years 2000 to 2999. An example, with invented numbers:

```
Usage report 2026-01-14 (local time, UTC+00:00)
Sources: 3 transcript file(s) read

Subscription
  Week: 31 % -> 37 % (+6 points), resets Mon 19 Jan 09:00
  5h windows: 1
    reset 13:00, peak 61 % at Wed 14 Jan 11:10 (first seen 08:00)

Total
  Requests: 49 (main 43, subagents 6)
  Tokens: input 1680, cache write 5m 44k, cache write 1h 120k, cache read 5.1M, output 178k
  Cost weight: 5.39 USD at API prices (main 5.39, subagents 0.01)
  Output: thinking 46k (30 %), web searches 3, web fetches 0
  Cache: hit rate 97 %, cold starts 1
  Subagents by type: reviewer 0.01 USD (6)
  Models:
    model Opus 5.5: 5.11 USD, 37 requests; thinking 46k (30 %), web searches 3, web fetches 0; cache hits 97 %
    model Sonnet 5.x: 0.28 USD, 6 requests; thinking n/a, web searches n/a, web fetches n/a; cache hits 98 %
    model Haiku 5.5: 0.01 USD, 6 requests; thinking n/a, web searches n/a, web fetches n/a; cache hits 97 %

Startup context
  Measured, the first request of a session or subagent (input + cache write + cache read):
    main: 2 starts, median 63k tokens, min 60k, max 66k, 2 warm (100 %)
    subagents by type:
      reviewer: 6 starts, median 38k tokens, min 36k, max 41k, 5 warm (83 %)
    per day:
      Wed 14 Jan: main 63k tokens (2 starts), subagents 38k tokens (6 starts)
  Estimate (~) from the files as they are now, for the folder of shop, at 2.3 characters per token:
    CLAUDE.md files: ~12k tokens (3 files)
      ~/.claude/CLAUDE.md: ~1500
      ~/.claude/rules/style.md: ~2700
      CLAUDE.md: ~7800
    MEMORY.md: ~3300 tokens (1 file)
    agent descriptions: ~7400 tokens (42 agents)
    skill descriptions: ~5600 tokens (61 skills)
    together ~28k tokens; not estimable: system prompt, built-in tools, MCP instructions, skills of plugins and other apps, output of hooks
    subagents with their own definition: reviewer: CLAUDE.md files ~12k tokens

By CLI (main session and its subagents, most expensive first; ~ = estimate)
  shop  5f3a9c21  started Wed 14 Jan 08:02  5.11 USD  37 requests  ~50.9 5h points  ~3.65 week points
    tokens input 1470, cache write 5m 30k, cache write 1h 120k, cache read 4.5M, output 155k
    models Opus 5.5 5.11
    output thinking 46k (30 %), web searches 3, web fetches 0
    cache hits 96.7 %, cold starts 1
  docs-site  c81e04d7  started Wed 14 Jan 08:26  0.28 USD  12 requests  ~3.1 5h points  ~0.35 week points
    tokens input 210, cache write 5m 14k, cache write 1h 0, cache read 555k, output 23k
    models Sonnet 5.x 0.28, Haiku 5.5 0.01
    output thinking n/a, web searches n/a, web fetches n/a
    subagents reviewer 0.01 USD (6)
    cache hits 97.5 %, cold starts 0

Allocation of the subscription rise (estimate)
  5h: 57 points measured, ~54.0 shared by cost between the CLIs, 3 unexplained (no request in between)
      0 stretch(es) not counted (a reset, or over 60 minutes between measurements), 0 points rise in them
  week: 6 points measured, ~4.0 shared by cost between the CLIs, 2 unexplained (no request in between)
      0 stretch(es) not counted (a reset, or over 60 minutes between measurements), 0 points rise in them

Calibration (USD per point, from stretches in which only one CLI was active)
  5h: not calibrated yet (30 of 50 stretches)
  week: not calibrated yet (30 of 50 stretches)

Gates
  spawn gate (shadow mode, only logged): allowed 7, warned 1, asked 0, denied 1
      warned by reason: call model: 1
      denied by reason: 5h window at N %: 1
  soft stop (shadow mode, only logged): warnings 1, denied 0
      warned by reason: checkpoint due: 1
```

- **Measured:** requests, tokens by kind (cache writes of 5 minutes and 1 hour apart), the share of the output that
  was thinking, web searches and web fetches, the cache hit rate, the cold starts, and the **cost weight**: what the tokens would cost at API prices
  (`tools/prices.json`, with its source and the date the prices were read). It compares sessions and models with
  each other; on a subscription it is not your bill. A model without an entry in the price file is listed as "no
  price" with its tokens, never estimated. Of the lines of one request (same message id and request id) only the
  last counts: it carries the complete output tokens. Thinking tokens are part of the output and already in its
  price, so they are shown but never added to the cost weight. The same figures are given per period, per model and
  per CLI. Where a transcript does not report a detail (older Claude Code versions), it is unknown, not zero: `n/a`
  in the text, `null` in the JSON; a total adds up only the requests that report it, and the thinking share is
  taken of the output of those requests.
- **Subscription:** the weekly value at the start and at the end of the period, and the 5h windows with their
  peak. They come from the measurement history; for the time before the history began, from the usage line of
  the usage clock in the transcripts (only what a hook added to the prompt counts: a file shown in the transcript
  that quotes the line does not). A weekly reset inside the period splits the week line into windows (`Week: 90 % ->
  100 % (+10 points) until Tue 13 Oct 14:00, then 0 % -> 12 % (+12 points)`), nothing is subtracted across the reset.
  The reset time is the recorded one, else derived from a known weekly reset time (the history's or the usage file's)
  in steps of seven days; with none, a fall of the value starts a new window. The JSON lists the windows as
  `week_segments` with `start`, `end` and `reset_at`.
- **Per CLI:** a CLI is a main session with its subagents, named by the folder, the first 8 characters of the
  session id and its start. A subagent is listed under its agent type: the spawned type, else the custom type, else
  the plain `agentType` of its `agent-*.meta.json`; a name given to a subagent is never shown (where `agentType` is only that name,
  the type is unknown). Besides tokens and cost weight: the cache hit rate (cache reads in all input) and the
  cold starts (a request of the main session, after the first, that writes at least 50 000 cache tokens).
- **Startup context:** what a session or subagent has read before it did anything of its own. **Measured:** the
  first request of every transcript (input + cache write + cache read tokens; it also holds the first prompt, so
  it is a little more than the fixed start); the median, smallest and largest start of the main sessions and of each
  subagent type, one row per day, and how many starts were *warm* (they read the prompt cache) against cold ones.
  A subagent's type is the one in the paragraph above; a session that is continued in a new transcript counts
  its first request there as a start. The JSON also lists every start (time, session, type, model, tokens) and
  never any content. **Estimated (`~`):** how the fixed part splits up, from the *size* of local files (their text
  is never read into the report or printed): the CLAUDE.md files a start in the folder of the latest start loads
  (`~/.claude/CLAUDE.md` and `rules/`, then from the outermost folder to the working folder `CLAUDE.md`,
  `CLAUDE.local.md`, `.claude/CLAUDE.md` and `.claude/rules/`; a rule with `paths:` in its frontmatter is not loaded at the
  start), `MEMORY.md` of the git repository (linked worktrees share it; else of the folder; main sessions only; the first 200 lines or 25 KB), and the `name` and `description`
  of the agents and skills of the user folder (`--claude-dir`, default `~/.claude`) and the project folders (from the
  working folder up to the root of its git working tree, as Claude Code looks for them), turned into
  tokens at `start_estimate.chars_per_token` (default 2.3, measured on German Markdown; English text needs about 4).
  Not estimable, and said so: the system prompt, the built-in tools, MCP instructions, skills of plugins and other apps,
  and the output of hooks. A subagent with `omitClaudeMd: true` in its definition has no CLAUDE.md part. The
  estimate is made only for `--full` and `--json`, for the files as they are now (an older start may have read
  other files), and is missing if no start of the period has a working folder that still exists. The archive keeps the
  starts of a finished day; a day archived by an earlier version has none and is read from the transcripts, as long as
  they exist. `--project` and `--session` show the measured starts of their sessions.
- **Allocation (marked with `~`, an estimate):** between two measurements the subscription percentages rose by
  some points; that rise is shared between the CLIs that made requests in between, by their cost weight. A rise
  with no request in between is **unexplained** (another app, a request without a transcript) and is not shared.
  Stretches that contain a reset, or last longer than 60 minutes, do not count. Attributed plus unexplained is
  always the counted rise. A fall of a value without reset times is taken for a stale number of an idle session
  (and that value is replaced by the last good one) only if it does not fall to half or less, a value as high as before
  comes back within 15 minutes and the last good value is at most an hour old; any other fall is a new window. Where a
  weekly reset time is known (the history's or the usage file's), any fall of the weekly value inside one weekly window is
  stale, and the whole measurement is left out. In the 5h windows without
  reset times, a window lasts five hours at most.
- **Calibration:** USD per point, a regression through zero over the stretches in which only one CLI was active. It
  regresses the rise on the cost and inverts the slope, because the rounding to whole points is in the rise, not in
  the cost.
  Below 50 such stretches it says "not calibrated yet". A model is listed only with 50 stretches of its own. In the text,
  a day is listed only with at least 20 stretches (the JSON lists every day with its count).
- **Small estimates:** an estimate below one point is written `<1`, not `~0.0` or `~1.0`: a session's share of a
  rise of a few points is not known to a tenth.
- **Gates:** how many starts the spawn gate allowed, warned about, asked about and denied, and the warnings and
  denials of the soft stop, per gate and mode (in `shadow` mode these are the decisions it would have made), and for
  every warning, question and denial **by reason**: the text of the gate's own log line before its first colon, with
  percentages written as `N %` (`checkpoint due`, `usage pause`). Days archived by an earlier version have no reasons; their
  decisions count as `reason not recorded`.

## The day archive

Claude Code deletes old transcripts (after 30 days by default). `--archive` keeps what a finished day showed: it
writes every finished day that is not in `<log_dir>/usage-days.jsonl` yet, never overwrites a day, and prints
nothing. Run it once a day, for example from cron (`5 0 * * * python3 /path/to/tools/usage_report.py --archive`)
or a systemd user timer. A finished day that the archive has is always reported from the archive (the transcripts of that day may be partly
deleted by now), so a month stays whole; a line of another format, or one that does not fit the figures, is not
an archive line (it is counted as unreadable and its day is computed from the transcripts). The
archive holds the figures only (no text), is private to you (mode 0600, folder 0700) and is opened without
following links.
