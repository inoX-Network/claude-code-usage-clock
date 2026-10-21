# The usage report: what every line and field means

`tools/usage_report.py` answers one question: what did my Claude Code usage go into? This file explains its output
in plain words, line by line for the text and field by field for `--json`, so that a person or an agent can read the
numbers without guessing. Where a number comes from, in which unit it is, and whether it was **measured** or is an
**estimate** is said for each of them.

The report reads only local files and makes no model call: the Claude Code transcripts (`~/.claude/projects`, only
token counts, models, times, folder names and ids, never message text), the measurement history
(`usage-history.jsonl`, written by the status line), the log of the two gates (`log.jsonl`), the day archive
(`usage-days.jsonl`) and, for the sessions of the period, the context split that the desktop plugin wrote after their
first answer (`context-breakdown/<session id>.json`, see *The context split* under `clis` below). Times
are local time.

## Words used

| Word | Meaning |
|---|---|
| **USD at API prices**, **cost weight** | What the tokens of a request would cost at the public API prices (`tools/prices.json`, with source and date). Measured from token counts, then multiplied by a price. It compares sessions and models with each other. On a subscription it is **not your bill**. A model without a price entry is not counted in USD and is listed as "without a price". |
| **`~`**, estimate | A number with `~` in front (or the column title `~week pts`) is an estimate, not a measurement. The subscription percentages are measured, but which session caused a rise is not: see **points**. |
| **points** | One point is one percentage point of the weekly limit (or of the 5h limit). The subscription page shows the limits as percentages; "+6 points" means the percentage rose by 6. |
| **week**, **weekly limit** | The subscription week that ends at the weekly reset time, not a calendar week. |
| **5h window** | A window of the five-hour limit. It has a peak: the highest percentage seen in it. |
| **session**, **CLI** | One main Claude Code session together with its subagents. It is named by the last name of its working folder and the first 8 characters of its session id (the **id**). |
| **project** | The name of the folder a session ran in: the same for everyone, without a setting, because the report shows where you worked. Sessions in the same folder are one project. |
| **subagent** | An agent started by a session. Its cost belongs to that session. It is listed under its agent type. |
| **start**, **startup context** | The first request of a main session or of a subagent. The tokens it carries (input + cache write + cache read) are everything read before the session did anything of its own, plus the first prompt. Measured. |
| **warm start**, **cold start** (of a start) | A start is *warm* if it read the prompt cache (cache read above 0), else *cold*. Not the same as `cold_starts` in `total`, which counts expensive requests later in a session. |
| **cache hit rate**, **cache hits** | Part of the input that was read from the prompt cache: cache reads divided by all input (input, cache writes, cache reads). Measured. A high rate is cheap. |
| **thinking** | The part of the output that was thinking. It is part of the output tokens and already in their price. Where a transcript does not say, it is unknown (`n/a` in the text, `null` in the JSON), not zero. |
| **unexplained** | A rise of the subscription percentage between two measurements in which no request with a price happened. It comes from something else (another app, a request without a transcript) and is not given to any project. |
| **gate**, **mode** | The `spawn gate` (start of agents) and the `soft stop` (checkpoint rule for subagents) of this tool. In mode `enforce` a denial was real; in `shadow` mode it is only what the gate would have done. |
| **reason** | Why a gate warned, asked or denied: the text of the gate's own log line before its first colon, starting in lower case, with percentages written as `N %`, other numbers as `N` and quoted names as `…` (for example `checkpoint due`, `usage pause`, `5h window at N %`). `reason not recorded` marks decisions of days that were archived before reasons were kept. |

## The text overview, line by line

The overview is the default output. Without a hit a block is left out.

```
Week to Mon 19 Oct 09:00 · 39 % of the weekly limit used (+39 points) · 100 USD at API prices
5h windows: 1, highest 82 % (Tue 13 Oct 14:30)
```

- **Line 1:** the period (`Day …`, `Week to <reset time>`, `Month …`, `Days a to b`); how far the weekly limit was
  used at the end of the period in percent (measured, from the status line), and by how many points it rose within
  the period (measured). If the period spans more than one weekly window, the line says `weekly limit at N % now ·
  +M points over K weekly windows`: N is the last window, M adds the rises of all of them; and the cost weight of everything
  in the period in USD at API prices (measured tokens times prices). Without a weekly measurement it says
  `no weekly measurement`.
- **Line 2:** how many 5h windows had a measurement in the period, and the highest peak among them in percent with
  the weekday, day and time of that peak. `none measured` if there was no measurement.

```
Where it went            USD   share  ~week pts  sessions
  webshop                 60    60 %       20.0         1
  …
  2 more                0.75    <1 %         <1         2
```

- **Where it went:** the five projects with the highest cost; the rest is one line `N more` that adds them up. A project
  name longer than 30 characters loses its middle for a `…` in this table (`my-company-frontend-admin-dashboard` is shown as
  `my-company-fron…dmin-dashboard`). If two projects would look the same after shortening, both are shown whole. Everywhere else
  (`--full`, `Details:`, the JSON) the name is whole.
- **USD:** the cost weight of the project in USD at API prices. Two decimals below 1 USD, one decimal below 10, none above.
- **share:** the project's part of the whole cost, in percent. `<1 %` is more than 0 and less than 1 %.
- **~week pts:** the estimated points of the weekly limit that fall to the project: the rise between two
  measurements is shared by the cost of the sessions that made requests in between, and the shares of a project's
  sessions are added. An estimate. `<1` is more than 0 and less than one point, `0` is none.
- **sessions:** how many sessions (CLIs) the project had in the period.

```
Main 75 USD · subagents 25 USD (top: stripe-meister 20 USD, ~6.6 week pts)
Opus 92 USD · Sonnet 8.0 USD · Haiku 0.75 USD · thinking 25 % of output · cache hits 89 %
```

- **Main … subagents …:** the cost weight of the main sessions and of all subagents, in USD at API prices.
  `top:` is the agent type of the subagents with the highest cost, with its estimated points of the weekly limit
  (`~6.6 week pts`; `<1` is more than 0 and less than one point, `0` is none). The points of all types are in `--full`
  and in `total`.
- **Opus … Sonnet … Haiku …:** the cost weight per model family in USD at API prices, highest first. A request of a
  model without a price is not in these numbers; `N request(s) without a price` says how many there were.
- **thinking … % of output:** the share of the output tokens that was thinking, among the requests that report it.
- **cache hits … %:** the cache hit rate of the whole period.

```
Start: main 45k tokens (median), subagents 36k-61k (median per type)
```

- **Start:** the size of the start in tokens (measured, see **start** above): `main` is the median over all main
  sessions that began in the period; `subagents` is the lowest and the highest of the medians of the agent types
  (one value if there is one type or the two are equal). Without main starts the part `main` is left out, without
  subagent starts the part `subagents`; without any start the whole line is left out. The sizes use `k` for
  thousands from 10 000 tokens on.

```
Notable
  soft stop denied 4 subagent tool calls (checkpoint due: 3, usage pause: 1)
  one 5h window reached 82 % (Tue 13 Oct)
```

- **Notable:** every line is one fixed rule that hit. The rules are, in this order (with the sync three more follow,
  see [`coverage.sync`](#coveragesync)):
  1. **A gate denied or asked** (`spawn gate`, `soft stop`; `agent starts` or `subagent tool calls`), per mode, with the
     number and the reasons (the three largest, then `other`). In `shadow` mode it says `would have denied` and
     `shadow mode, nothing was blocked`.
  2. **A 5h window reached `thresholds.start_block_5h`** (default 75 %) or more: the number of such windows and the highest peak.
  3. **`cache hits under 90 %`:** the projects with a cache hit rate below 90 %, with the rate rounded down.
  4. **One project made more than half of the cost:** its share and its cost weight.
  5. **`unexplained rise`:** points of the subscription that rose with no request with a price in between, for the
     week and for the 5h limit.

```
Details: --project webshop · --session aaaaaaaa · --full
```

- **Details:** the options with which to see more, filled in with the project of the highest cost and its largest
  session. `--project NAME` (the whole name or the beginning of it) and `--session ID` (the beginning of a session id) print
  the blocks of `--full` for those sessions and their sum. If more than one project or session fits, or none, the report
  lists the candidates and stops with exit code 2. `--full` prints everything.

## `--full`

`--full` prints the full report. In `Total`, `Main session: ~x pts ~y pts 5h` and `Subagents by type: reviewer 0.01 USD (6)
~x pts ~y pts 5h, …` give the estimated points of the weekly limit (`pts`) and of the 5h limit (`pts 5h`) of the main
sessions and of each agent type, shared by cost (an estimate below one point is written `<1`). The sections are `Subscription`, `Total`, `Startup context`, `By CLI`, `Allocation`,
`Calibration` and `Gates`, in the same order as the JSON sections below, with every figure. In it, an estimate of a session below one
point is written `<1`; a 5h window names the weekday and date of its peak; the calibration lists a day only with at
least 20 stretches; each gate is listed with its mode and, per warning, question and denial, with its reasons.

### `Startup context` in `--full`

```
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
    MEMORY.md: ~3300 tokens (1 file)
    agent descriptions: ~7400 tokens (42 agents)
    skill descriptions: ~5600 tokens (61 skills)
    together ~28k tokens; not estimable: system prompt, built-in tools, MCP instructions, skills of plugins and other apps, output of hooks
    subagents with their own definition: reviewer: CLAUDE.md files ~12k tokens
```

- **Measured part (no `~`):** per group the number of starts, the median, smallest (`min`) and largest (`max`) size
  in tokens, and how many starts were `warm` (read the cache) with their share. `main` are the main sessions,
  `subagents by type` the subagents by their agent type (most starts first), `per day` one row per local day, the
  median over all subagent types together. The time of a start is that of the last line of its request.
- **Estimate (`~`):** how the fixed part of a start could split up. It is made from the *sizes* of local files (the
  text is not printed; the user folder is `--claude-dir`, default `~/.claude`): characters divided by
  `start_estimate.chars_per_token`. `CLAUDE.md files` lists each file the
  folder loads at the start (`~/.claude/…` is the user folder, other names are relative to the working folder; a rule
  with `paths:` in its frontmatter is not loaded at the start and not counted). `MEMORY.md` is the memory index of the git repository (a linked worktree shares the one of its main working tree) or of the
  folder; main sessions load it, subagents do not (first 200 lines or 25 KB). `agent descriptions` and `skill
  descriptions` count the name and description of each definition. `not estimable` is what only Claude Code knows.
  The last line gives, for the subagent types of the period that have a definition, their share of the CLAUDE.md
  files; with `omitClaudeMd: true` they read none. The estimate uses the files as they are now and the folder of the
  latest start of the period that still exists; it is `not available` if there is none. `--project` and `--session`
  print the measured part for their sessions only.

## `--json`

`--json` prints one object. It contains every figure of `--full` and the `overview`; it is the same data the text
is made of. `--full`, `--project` and `--session` do not apply to it (each CLI carries its `label`, the project, so a reader can
filter). Percentages are numbers from 0 to 100 (`used_percent`, `peak`, `highest_percent`, `threshold_percent`); a
`share` or `*_rate` is a number from 0 to 1. Costs ending in `_usd` are USD at API prices. Times are UTC in the form
`2026-10-13T12:30:00Z`, except `reset_at` and `resets`, which are Unix seconds (in every section). A value that is not known is `null`.

The top-level fields are, in this order:

| Field | Meaning |
|---|---|
| `schema_version` | The number of the format (now `1`). It changes only when a field is renamed or removed; a new field does not change it. |
| `period` | `kind` (`day`, `week`, `month`, `year` or `range`), `label` (as in the title), `start` and `end` (the period is from `start` up to, not including, `end`). |
| `coverage` | What was read: `transcript_files` (count), `archive_days` (days that came from the archive), `archive_days_without_agent_split` (the days among them whose points could not be split by agent type, see `total`), `unreadable_history_lines`, `unreadable_archive_lines`, `unreadable_transcript_lines` (counts of lines skipped because they could not be read), `unreadable_context_files` (count of context files, among those of the period's sessions, that were skipped because they could not be read or do not fit the format); with the sync `sync` (see [`coverage.sync`](#coveragesync)). |
| `overview` | The numbers of the text overview, see below. |
| `subscription` | The measured percentages of the subscription, see below. |
| `total` | The figures of the whole period. |
| `startup` | What the sessions and subagents of the period read at their start, see below. |
| `clis` | The figures of each session, highest cost first. |
| `allocation` | How the rises of the subscription were shared between the sessions. |
| `calibration` | USD per point, a cross-check of the sharing. |
| `guards` | How many decisions each gate made, by gate, mode and decision. |
| `guard_reasons` | The same, by reason. |

### `overview`

Measured unless it says estimate. The text overview is made of this object alone.

| Field | Meaning |
|---|---|
| `title` | The period as the first line says it (`Day 2026-10-13`, `Week to Mon 19 Oct 09:00`, `Month 2026-10`, `Days a to b`); with `--machine` followed by ` · machine NAME`. |
| `cost_usd` | The cost weight of the period, USD at API prices. |
| `week` | `null` without a weekly measurement. Else `used_percent` (the weekly limit used at the end of the period, percent), `rise_points` (points it rose within the period, across weekly resets added up), `windows` (how many weekly windows the period touches) and `reset_at` (the weekly reset time of the last one in Unix seconds, or `null`). |
| `five_hour` | `windows` (count of 5h windows with a measurement), `highest_percent` and `highest_at` (the highest peak and its time; `null` without windows). |
| `projects` | Every project, highest cost first (ties by name): `name` (the whole folder name); `cost_usd`; `share` (part of the whole cost, 0 to 1, `null` if the whole cost is 0); `week_points` and `five_points` (estimates: the points of the weekly and of the 5h limit that fall to the project); `measured_share` (see `clis`, weighted by cost); `sessions` (count); `session_ids` (the 8-character ids, highest cost first); `cache_hit_rate` (0 to 1, `null` without input tokens). |
| `max_projects` | How many projects the text shows as rows (5). |
| `more` | `null`, or the sum of the projects beyond `max_projects`: `projects` (count), `cost_usd`, `share`, `week_points`, `five_points`, `measured_share`, `sessions`. |
| `split` | `main_usd` and `subagents_usd` (cost weight of the main sessions and of the subagents) and `top_subagent` (`type`, `cost_usd` and `week_points` (estimate, see `total`) of the subagent type with the highest cost, or `null`). |
| `models` | `families`: a list of `name` (the first word of the model name in `tools/prices.json`, for example `Opus`) and `cost_usd`, highest first; `unpriced_requests`: how many requests had a model without a price (they are not in any USD figure). |
| `thinking_share` | Part of the output that was thinking (0 to 1), among the requests that report it; `null` if none does. |
| `cache_hit_rate` | The cache hit rate of the period (0 to 1, `null` without input tokens). |
| `startup` | `null` without any start. Else `main_median_tokens` (median start of the main sessions in tokens, `null` without main starts) and `subagent_type_median_tokens` (`min` and `max` of the medians of the subagent types in tokens, `null` without subagent starts). Measured. |
| `notable` | A list, one entry per hit of a fixed rule, in the order of the text. Each has `rule` and `text` (the line as printed), and the numbers of its rule: see below. |
| `details` | `project` and `session`: what the `Details:` line suggests (both `null` without projects). |

The entries of `notable` by `rule`:

| `rule` | Further fields |
|---|---|
| `gate_denied`, `gate_asked` | `gate` (`spawn_gate` or `soft_stop`), `mode`, `count`, `reasons` (a map from reason to count, with `reason not recorded` for decisions archived without one). |
| `five_hour_window` | `windows` (count of windows at or above the threshold), `threshold_percent`, `highest_percent`, `highest_at`. |
| `cache_hits_low` | `threshold` (0.9) and `projects`: a list of `name` and `cache_hit_rate`. |
| `project_dominates` | `project`, `share` (above 0.5) and `cost_usd`. |
| `unexplained_rise` | `week_points` and `five_points`: points that rose with no request with a price in between (an entry exists if one of them is above 0). |
| `sync_stale` | With the sync: `machines`, a list of `name` and `last_success` (`null`: never) of the machines without a successful push for over 24 hours, this one included. |
| `sync_timezone` | With the sync: the machines whose `utc_std` is known have at least two different ones. `machines`, a list of `name` and `utc_std` of those machines; the text names each with its offset (`UTC+1`, `UTC-8`, `UTC+5:30`). All machines should run in one time zone; otherwise day lines are cut at each machine's local midnight. The figures are not changed. |
| `sync_backoff` | With the sync: this machine's push is blocked (`category` `visibility`: the repository was found public, `next_due` `null`) or backs off after an error (`category`, `next_due`: when it tries again). |
| `sync_machine_id` | With the sync: the machine id of this machine (`sync/machine_id` in the log folder) cannot be used; the other machines are not read and this one does not push. |
| `sync_pull` | With the sync: the pull before the report failed (`status` `failed`, `category`) or could not bring the histories together (`status` `diverged`: the own commits do not fit onto the repository, or the clone has changes that are not committed; nothing was merged). The text names the fixed reason. The other machines are as of the last pull. |

### `subscription`

Measured, from the measurement history (for the time before the history began, from the usage line of this tool in the transcripts).

| Field | Meaning |
|---|---|
| `week_segments` | The weekly windows in the period, each with `start` and `end` (`at` and `value`: time and weekly percentage of the first and last measurement) and `reset_at` (the weekly reset time in Unix seconds, or `null`). A weekly reset splits the list; nothing is subtracted across it. |
| `five_windows` | The 5h windows: `resets` (the reset time in Unix seconds, or `null`), `first_seen`, `peak` (highest percentage) and `peak_at`. |

### `total`

| Field | Meaning |
|---|---|
| `requests` | Number of requests (of one request only the last line of the transcript counts: it holds the full output tokens). |
| `tokens` | Token counts by kind: `input`, `cache_write_5m`, `cache_write_1h` (tokens written to the cache for 5 minutes and 1 hour), `cache_read`, `output`. |
| `cost_usd` | The cost weight, USD at API prices. |
| `main`, `subagents` | `requests` and `cost_usd` of the main sessions and of the subagents. `subagents` also has `by_type`, which maps each agent type to `requests` and `cost_usd`. `main` and every type in `by_type` also have `week_points` and `five_points`: the **estimated** points of the weekly and of the 5h limit that fall to them, shared by cost like the points of the sessions (see `allocation`). A type whose requests lie in a stretch that began before the period can have points and `requests` 0. The points of `main` and of all types add up to `attributed_points` of `allocation` for each limit (to two decimals), except for the days named in `coverage.archive_days_without_agent_split`. A type that the transcript does not name is `unknown`, an odd name `other`; a name is never shown. |
| `models` | Per priced model name: `requests`, `cost_usd`, `tokens`, `cache_hit_rate`, `thinking_tokens`, `thinking_share`, `web_searches`, `web_fetches`. |
| `unpriced` | The same for models without a price, without `cost_usd`: their tokens are shown, never estimated. |
| `cache_hit_rate` | Cache reads divided by all input, 0 to 1 (`null` without input). |
| `cold_starts` | Requests of a main session, after its first, that wrote at least 50 000 tokens to the cache. They are expensive: the cache had run out. |
| `thinking_tokens`, `thinking_share` | Thinking tokens (part of the output) among the requests that report them, and their share of those requests' output; `null` if none reports. |
| `web_searches`, `web_fetches` | Counts of web searches and fetches among the requests that report them; `null` if none reports. |

### `startup`

The starts of the period: the first request of every main session and subagent transcript that began in it. Sizes
are in tokens. Everything is measured except `estimate`.

| Field | Meaning |
|---|---|
| `starts` | Every start, oldest first, never any content: `at` (time of the last line of the request), `session` (the 8-character id of its session, as in `clis`), `type` (the agent type, `null` for a main session; `unknown` or `other` where the transcript does not say), `model` (the name in the price file, else the model id, else `?`) and `tokens`: `input`, `cache_write` (5 minutes and 1 hour together), `cache_read` and `total` (their sum). |
| `main` | The main-session starts as one group, or `null`. A group has `starts` (count), `median_tokens` (rounded), `min_tokens`, `max_tokens`, `warm_starts` (how many read the cache) and `warm_share` (0 to 1). |
| `subagents` | `all` (the group of all subagent starts, or `null`) and `by_type`: each agent type to its group, most starts first. |
| `per_day` | One entry per local day with starts: `day` (`2026-10-13`), `main` and `subagents` (groups, `null` if there was none that day; `subagents` are all types together). |
| `estimate` | `null` if none could be made (only made for `--full` and `--json`). Else the estimate, see below. |

The `estimate` (all numbers are estimates, from the sizes of local files as they are now):

| Field | Meaning |
|---|---|
| `project` | The name of the folder it was made for: that of the latest start in the period whose folder still exists. |
| `chars_per_token` | The factor used (`start_estimate.chars_per_token`, default 2.3). |
| `parts` | Four entries in this order, each with `name`, `items` (count), `chars` (characters), `tokens` (`chars` divided by `chars_per_token`, rounded). `claude_md`: the CLAUDE.md and rule files read at the start; `memory`: MEMORY.md of the folder (main sessions only); `agents` and `skills`: the name and description of each definition. `claude_md` and `memory` also have `files`: a list of `file` (the name, relative to `~/.claude` or to the working folder), `chars` and `tokens`. |
| `total_tokens` | The sum of the `tokens` of the four parts. |
| `not_estimable` | What only Claude Code knows and the report does not guess: the system prompt, the built-in tools, MCP instructions, skills of plugins and other apps, the output of hooks. |
| `subagents` | For each subagent type of the period that has an agent definition: `type`, `omit_claude_md` (`true` if the definition says `omitClaudeMd: true`) and `claude_md_tokens` (the `tokens` of `claude_md`, 0 with `omit_claude_md`). |

### `clis`

One entry per session, highest cost first. `id` (8 characters of the session id), `label` (the working folder's last name: the project of the session; for a session of another machine its project label in the sync repository),
`machine` (with the sync only: the name of the machine the session ran on),
`start` (time of its first request), `requests`, `cost_usd`, `tokens` (as in `total`), `models` (model name to
cost in USD), `subagents` (agent type to `requests` and `cost_usd`), `cache_hit_rate`, `cold_starts`, `thinking_tokens`,
`thinking_share`, `web_searches`, `web_fetches` (as in `total`), `estimated_share` with `five` and `week`: the
**estimated** points of the 5h and of the weekly limit that fall to this session, `measured_share` with `five` and
`week`: the part of its cost (0 to 1, `null` at a cost of 0) that lay in a counted section of that limit (see
`allocation`), so that `0` there means its points are **not measured**, not zero (the text then writes `-`), and
`context`: how the context of
the session was made up after its first answer, or `null` (see the next section).

### The context split (`clis[].context`)

The desktop plugin measures it once per main session, after the first answer, with the same call `/context` uses, and
writes it to `<log_dir>/context-breakdown/<full session id>.json`. The transcripts hold only the sum of each request,
so this is the only place that says what the start of a session consisted of. **Measured** by Claude Code, not
estimated; only names and numbers, never the text of a prompt, a file or a tool. Subagents have no file of their own.

`context` is the content of the file of the session, or `null` if there is none (the session began before the plugin
wrote files, the plugin is not installed, or the measurement failed). A file belongs to the CLI whose `id` is the first
8 characters of the file's session id, the same rule `--session` uses. The report reads only the files of the
sessions in the period. A file that is not JSON, is larger than 4 MB, has another `schema_version`, lacks a required
field or has a field of the wrong type is not used: `context` is `null` and `coverage.unreadable_context_files` counts
it. The exit code does not change. A field the format does not name is not passed on, a name or path longer than 200
characters is cut and a control character in it becomes `?`.

The fields of the file (`schema_version` 1), all token counts whole numbers:

| Field | Meaning |
|---|---|
| `schema_version` | `1`. The report reads only this version. |
| `session_id` | The full session id. Required in the file, and it must equal the file name; the report leaves it out of `context` (the CLI's `id` is the start of it). |
| `at` | When it was measured, as the plugin wrote it (ISO time, text). |
| `cwd`, `model`, `detail` | The working folder, the model and the level of the measurement as the plugin reports it (`full` or `summary`). Each may be missing or `null`. |
| `total_tokens`, `max_tokens` | The tokens the context held at that moment and the size of the window. |
| `categories` | The parts `/context` names, in its order: `name`, `tokens` and `kind` (the kind the plugin reports, may be `null`). |
| `memory_files` | CLAUDE.md and memory files: `path` (as written; the text report shortens the home folder to `~`), `type` (may be `null`) and `tokens`. |
| `mcp_tools` | Each MCP tool: `server`, `name`, `tokens` and `loaded` (whether the tool was loaded into the context, as reported; may be `null`). |
| `skills` | `null` if the engine gave no split of the skills (the key must still be there). Else `tokens` (all skills), `included` (how many were in the context), `total` (how many exist) and `items`: `name` and `tokens` of each one. |
| `agents` | Each agent definition: `type`, `source` (where it comes from, may be `null`) and `tokens`. |

`schema_version` of the **report** stays 1: `context` and `unreadable_context_files` are new fields, and a new field does
not change it (see the table above).

With `--session` the text report shows the same data as the block *Context after the first answer*: the total and the
window with the percentage, the categories, the MCP servers (tokens of all their tools, number of tools and their ten
dearest tools), the memory files, the skills (tokens, how many were included of how many exist, the ten dearest; `Skills: no skill breakdown` if the file has none) and the
agents (the ten dearest); every list has the dearest row first and ends with `… N more` if rows were left out. Without a
file the block is the one line `No context recorded for this session.`; an unreadable file is named in `Sources:` as
`N unreadable context file(s) skipped`. `--project` alone and the overview do not show the block.

### `coverage.sync`

Only with the sync (`sync.mode` `shadow` or `on`, not with `--local`). Without it neither this object nor
`clis[].machine` is in the report. How the report merges the machines: [`usage-report.md`](usage-report.md#other-machines-the-sync).

| Field | Meaning |
|---|---|
| `mode` | `sync.mode`: `shadow` or `on`. |
| `text` | The part of the `Sources:` line of `--full` about the sync, for example `Sync: 3 devices · desk 4 min ago · laptop 3 h ago · pull ok`: the number of machines, each with the age of its last successful push (`never pushed` without one; the machines of one name as one part, `claude-web (12 devices) 2 h ago`, with the newest push), the push of `--sync`, the pull, and `only NAME` with `--machine`. |
| `machine` | The name `--machine` chose (all machines of that name count), else `null`. |
| `machines` | Every machine, this one first, then by name: `id` (its machine id; `null` if this machine never synced), `name` (`sync.machine_name`, else `machine-` and 6 characters of the id; from another machine's `machine.json`, with control characters as `?` and at most 64 characters), `platform` (`linux`, `darwin`, `windows`, `null` if unknown), `own` (`true` for this machine), `last_success` (its last successful push, from the state of this machine or the `status.json` of the other; `null`: none), `utc_std` (the offset of its standard time to UTC in minutes: of this machine from its time zone, of another from its `machine.json`; `null` if that is missing or cannot be read). |
| `pull` | `status`: `ok` (pulled now), `recent` (the last pull is less than 5 minutes old: none now), `skipped` (`--no-pull`), `locked` (another sync run holds the lock: the clone as it is), `failed` or `diverged` (the histories could not be brought together: nothing merged); `category` (the error category of a failed pull, else `null`); `detail` (the fixed text of that error, never output of git, for example `the sync clone is missing, run the installer with --sync-repo`; else `null`); `last_pull` (the last successful pull, `null`: never). |
| `push` | `null`, or with `--sync`: `status` `ok`, `failed` (with `category`), `blocked` (`category` `visibility`: the repository is public, no push) or `locked`; `category`; `detail` (the fixed text of a failure, never output of git, else `null`). |
| `clone` | `read` (the clone was read), `missing` (there is no clone: only this machine; `Sources:` says `no clone of the sync repository`) or `unread` (the machine id of this machine cannot be used, so its own folder could not be told from the others: the clone is not read; `Sources:` and `Notable` say so). |
| `days_from_lines` | The day lines that count in the period instead of the requests of their machine (see [`usage-report.md`](usage-report.md#other-machines-the-sync)): a list of `day` and `machine` (its name). |
| `backoff` | The push state of this machine: `level` (0 without a backoff), `last_error` (a category or `null`), `next_due` (when the next push is due, `null`: at once). |
| `skipped` | What the reader skipped in the clone, by counter (`symlinks`, `hardlinks`, `oversized`, `irregular`, `bad_names`, `unreadable`, `unknown_version`, `broken_lines`, `misplaced`, see [Reading](#reading)). `--full` names the sum in `Sources:`. |
| `unreadable_day_lines` | Day lines of other machines that were skipped: the rules of the day archive do not accept them, a number is not finite, negative or above 10^12, a name or text has control characters or more than 64 characters, `models`, `unpriced`, `agent_types` or `clis` are not in the shape the report reads, the whole report cannot be made of them, or they do not add up with the other figures of their day. |

### `allocation`

For `five` and `week`, each with: `counted_points` (the rise between measurements, measured, in stretches that count),
`attributed_points` (the part of it that was shared between sessions: an estimate), `unexplained_points` (the part
with no request with a price in between, shared with nobody), `skipped_sections` and `skipped_points` (stretches that
contain a reset or are longer than 60 minutes do not count; how many, and the rise in them). `attributed_points` plus
`unexplained_points` is always `counted_points`. The same shares by agent type are in `total` (`main` and `subagents.by_type`: `week_points`, `five_points`).

### `calibration`

For `five` and `week`: `status` (`calibrated` or `not calibrated yet`), `sections` (the number of stretches in which only
one session was active), `usd_per_point` (USD of cost weight per point, `null` until 50 such stretches), `per_day` and
`per_model` (a map from a day, or a model, to `sections` and `usd_per_point`; a model only from 50 stretches). It lets
you check the sharing: the value should be about the same on every day. The text lists a day only with at least 20 stretches; the
JSON lists every day with its count.

### `guards` and `guard_reasons`

`guards` maps each gate (`spawn_gate`, `soft_stop`) to its modes (`shadow`, `enforce`, or `unknown`) and each mode to
the number of decisions by kind: `allow`, `warn`, `ask`, `deny` (`other` for a decision the log could not name).
`guard_reasons` has the same shape with one more level: for `warn`, `ask` and `deny` a map from reason to count.
Decisions of days archived before reasons were kept are in `guards` but not in `guard_reasons`.

### `side_calls`

Only with the sync and side lines of a machine of Claude Code on the web in the period (see
[the sync](sync.md#claude-code-on-the-web)); otherwise the key is missing, and so is `overview.side_calls`. None of it
is in `total`, `clis` or the allocation.

| Field | Meaning |
|---|---|
| `sources` | One entry per source of side calls, by name. |
| `source` | The source as the debug log names it, e.g. `extract_memories`. |
| `runs` | Runs of the source in the period, each `k` once. |
| `calls` | Their requests to `/v1/messages`. |
| `tokens` | `input`, `cache_write`, `cache_read`, `output` of those runs. |
| `cost_usd` | At API prices, the cache write priced as 1 hour (an estimate): per source and in all. |
| `unpriced_runs` | Runs without a model or with a model without a price: counted, not priced. |
| `sessions` | Sessions with at least one window of an export. |
| `sessions_without_log` | Of them, those whose exports never had a debug log. |
| `requests_not_measured` | Requests of the period of those sessions outside every span of a debug log: their side calls are not measured. |
| `unmeasured_calls` | Calls the newest window of each machine and session counted without token counts. |

`overview.side_calls` holds `cost_usd`, `runs` and `requests_not_measured` for the line of the text overview
(`Side calls outside the transcripts: ~0.62 USD (1 run), 2 requests not measured · not in the sums · --full`).

## The sync repository

With the sync, every machine writes its usage data to a private git repository of the user, and the report of each
machine reads the folders of the others. `hooks/sync_format.py` is the format of that repository: what is written, how
it is laid out, and how it is read. Each machine writes only below its own folder:

```
machines/<machine_id>/
  machine.json
  status.json
  requests/<YYYY-MM>/<session_id>.jsonl
  history/<YYYY-MM>.jsonl
  gates/<YYYY-MM>.jsonl
  days/<YYYY-MM>.jsonl
  side/<YYYY-MM>.jsonl
  side_windows/<YYYY-MM>.jsonl
```

- `<machine_id>` is a UUID4, made at the first activation and kept in `<log_dir>/sync/machine_id` (mode 0600). A
  folder whose name is not a UUID4 is not read.
- `requests/<YYYY-MM>/<session_id>.jsonl`, `history/<YYYY-MM>.jsonl` and `gates/<YYYY-MM>.jsonl` are named after the
  month of the line's `t` in UTC, `days/<YYYY-MM>.jsonl` after the month of its `day`. No file name has a colon; times
  are only in the lines.
- Every `.jsonl` file is UTF-8 with one record per line and is only appended to. `status.json` and `machine.json` are
  the only files that are overwritten, and only by their own machine: `status.json` on every push, `machine.json` only
  when `name`, `platform` or `utc_std` changed (it keeps its `created`). Old request months are compacted (see below).
- Times are UTC in the form `2026-10-13T12:30:00Z`, with three decimals of a second where the transcript has them
  (`2026-10-13T12:30:00.250Z`).

Every line and file has `v`, the version of its format (now `1`). A reader skips a line of another version.

### `machine.json` and `status.json`

| Field | Meaning |
|---|---|
| `v` | The version of the format, `1`. |
| `name` | The name of the machine: `sync.machine_name`, or `machine-` and the first 6 characters of its id if that is empty. The host name is never used. A changed name rewrites `machine.json`. |
| `platform` | `linux`, `darwin` or `windows`. |
| `schema` | The version of the line formats the machine writes (now `1`), in both files. |
| `created` | When the machine first wrote to the repository. |
| `utc_std` | The offset of the machine's local standard time to UTC in minutes, without daylight saving time (Germany `60`, `-time.timezone // 60`): a whole number from `-720` to `840`. It does not change when the clocks change; a move to another time zone rewrites `machine.json`. A `machine.json` without it or with a value outside that range is not read: the report then names the machine by its id. |
| `last_success` | `status.json`: the time of the machine's last successful push. |
| `tool_version` | `status.json`: the version of the usage clock that pushed. |

### Request lines (`requests/<YYYY-MM>/<session_id>.jsonl`)

One line per request of a transcript, made by the same rules as in the local report (of the lines of one request the
last counts; the type of a subagent; the first request of a transcript). A request that changes later is appended again
with the same `k`: a reader keeps, per `k`, the line with the most output tokens (`out`), as the local report does. Of
lines with the same `out` it keeps the one of the session whose first request is earliest (a request copied into a
continued session belongs to the session it came from), then the one of the smaller session id, and within one session
the one it read last. So neither the sums nor the session of a request depend on the order in which files are read.

| Field | Meaning |
|---|---|
| `v` | `1`. |
| `t` | The time of the request. |
| `k` | The key of the request: the first 16 hex characters of SHA-256 of the message id, a line break and the request id. The ids themselves are not written. |
| `sid` | The session id. |
| `sub` | `null` for the main session, else the agent type of the subagent as the local report names it (`unknown` or `other` where the transcript does not say). |
| `proj` | The project label after `sync.project_labels`, see below. |
| `model` | The model name as in the transcript; a name with characters outside `A-Z a-z 0-9 _ . : / -` or longer than 64 is written as `?`. |
| `in`, `cw5`, `cw1h`, `cr`, `out` | Tokens: input, cache write for 5 minutes, cache write for 1 hour, cache read, output. Whole numbers. |
| `think`, `ws`, `wf` | Thinking tokens, web searches, web fetches; `null` where the transcript does not report them. |
| `first` | `true` for the first request of a transcript (a start, see `startup`). |

`sync.project_labels` decides what `proj` holds, and the project names in the day lines (`stats.clis[*].label`):

- `plain` (the default): the folder name as the local report shows it. Everyone in the private repository sees the
  project names.
- `alias`: the entry of the folder in `sync.project_aliases`, looked up by the folder name as it is, then as the report
  shows it; a folder without one becomes `unbenannt-` and the first 8 hex characters of SHA-256 of its name.
- `hash`: `p-` and the first 12 hex characters of SHA-256 of `sync.label_salt`, a line break and the folder name.
  Without a salt the export stops with the error category `internal`; it never falls back to `plain`.

### Measurement, gate and day lines

| Field | Meaning |
|---|---|
| `five`, `week`, `five_resets`, `week_resets` | `history/`: one measurement, as in `usage-history.jsonl` (see `subscription`): the 5h and weekly percentage and their reset times in Unix seconds, each `null` if unknown, never negative; with `v` and `t`. |
| `hook` | `gates/`: the gate, `spawn_gate` or `soft_stop`; with `v` and `t` (the time of the decision). |
| `decision` | `gates/`: the decision as the local log names it, read the way the local report reads it: `allow`, `warn`, `ask`, `deny`, and `error` for a soft stop that failed; `other` if the log holds no lower-case word of at most 20 letters. |
| `mode` | `gates/`: the mode as the local log names it, `shadow` or `enforce`; `unknown` if the log holds no lower-case word. |
| `reason` | `gates/`: the text of the local log line before its first colon, quoted parts as `…`, percentages as `N %`, other characters outside a small set as `?`, at most 60 characters. The question of the ask dialog, which names the agent, is written as `7-day window at N %`. Never more than the local log holds. |
| `day`, `stats` | `days/`: the lines of the day archive `usage-days.jsonl`: `v`, `day` (the local day) and `stats` (the figures of that day, see *Mergeable figures and the archive* in `docs/architecture.md`). They are taken over as they are, with one exception: the project name of each session (`stats.clis[*].label`, the folder name) becomes its label after `sync.project_labels`, the same as `proj`. With `plain` the line is the archive line byte for byte; with `alias` or `hash` no folder name is left in it. A `clis` that is not an object of entries with a text `label` stops the export (category `internal`). `sync_format.py` checks only the envelope (`v`, `day`, `stats` an object); the report checks the figures in `stats` with the rules of its day archive before it uses them. |

### Side calls (`side/<YYYY-MM>.jsonl`, `side_windows/<YYYY-MM>.jsonl`)

Only a machine of Claude Code on the web writes them, through `tools/web_export.py` (the skill `/usage-export`, see
[the sync](sync.md#claude-code-on-the-web)). Some calls of Claude Code never reach the transcript: the memory pass
after each answer (`extract_memories`) reads the whole cached context once more. The debug log of the session names
them, with their token counts; the export takes only those numbers from it. Both files are named after the month of
`t` in UTC.

| Field | Meaning |
|---|---|
| `t` | `side/`: when the run finished; `side_windows/`: when the export read the debug log. |
| `k` | `side/`: the key of the run, the first 16 hex characters of SHA-256 of `sid`, `src` and `t`, each followed by a line break except the last. A run exported twice counts once. |
| `sid` | The session id. |
| `src` | `side/`: the source as the debug log names it (`extract_memories`), a lower-case word. |
| `model` | `side/`: the model the debug log names for the calls of the run, `null` if it does not. |
| `calls` | `side/`: the requests of the run to `/v1/messages`. |
| `cw` | `side/`: tokens of the cache write, without the split into 5 minutes and 1 hour (the debug log does not give it; the report prices it as 1 hour, as an estimate). `in`, `cr` and `out` as in the request lines. |
| `log` | `side_windows/`: `true` if the session had a debug log. `false` means nothing was measured, which is never the same as zero. |
| `from`, `to` | `side_windows/`: the first and the last time in the debug log, `null` without a log. Requests of the session outside this span have no measurement of their side calls. |
| `unmeasured` | `side_windows/`: requests to `/v1/messages` in the debug log outside the transcript whose source gives no token counts. |

### What is never written

The writer takes over only the fields above and checks each value; any other field is dropped. No prompt, message
text, command, path, file name, host name (except a `name` the user chose), raw message or request id, and no content
of CLAUDE.md, skills or agents. A value that does not fit the format stops the export (category `internal`).

### Reading

Everything below `machines/` counts as foreign data. A reader takes only regular files (checked with `lstat`) with a
single link and names of the layout, at most 8 MB per file, and never a path outside the repository folder. A line
must lie in the file of its month (`t`, or `day` for a day line). It never runs anything and ignores executable bits.
What it skips it counts:

| Counter | What was skipped |
|---|---|
| `symlinks` | A link, as a file or as a folder. |
| `hardlinks` | A file with more than one hard link (it could be shared with a file outside the repository; git never makes one). |
| `oversized` | A file larger than 8 MB, also one that grew past 8 MB while it was read. |
| `irregular` | Anything that is neither a regular file nor a folder (a named pipe, a device), also a file that turned into one after it was checked. |
| `bad_names` | A file or folder whose name is not one of the layout, or that resolves outside the repository folder. |
| `unreadable` | A file or folder that could not be opened. |
| `unknown_version` | A line or file with another `v`. |
| `broken_lines` | A line or file that is not UTF-8 JSON or does not fit its format. |
| `misplaced` | A line in the file of another month: a request, measurement or gate line whose `t`, or a day line whose `day`, is not in the month the file or folder is named after. |

### Compacting (`sync.keep_request_months`)

A machine removes month folders `requests/<YYYY-MM>/` from its own folder once they are older than
`sync.keep_request_months` full months (default 3, at least 1; with 3, in October the months up to June go, July to
September and October stay), as a commit in the checked-out repository. A month goes only if, for every local day on
which it has requests, a day line lies in the file of its month in `days/`; if one is missing, or anything in the month
could not be read or lies in the wrong month, it stays. The day
lines stay. A compacted month therefore has, in a report, only the local split of each machine from its day line, no
split across machines. The git history is never rewritten.

## Known limits

- **Projects are folder names.** The report knows no paths, on purpose. Two folders of the same name in different places are
  one project.
- **A start is the first request of a transcript.** A session that is continued in a new transcript has a start there
  with the old context in it. The first prompt is part of a start, and the output of the hooks that ran at the start.
- A day archived by an earlier version, before the points by agent type were kept, has none in the archive; they are
  computed from the transcripts, which Claude Code deletes after about 30 days. A day for which the transcripts do not hold
  all that the archive says was attributed is named in `coverage.archive_days_without_agent_split` (and in the `Sources:`
  line of `--full`): its points are missing from `main` and the types, not from `attributed_points`.
- A day archived before the starts were kept (version 1.1.1 and earlier) has none in the archive; they are read from
  the transcripts, which Claude Code deletes after about 30 days.
- The estimate does not see `@` imports inside CLAUDE.md files, plugin agents, or what an older start read before a file changed.
- An estimate (`~`) is a share of a measured rise, not a measurement. Percentages of the subscription are whole points.
