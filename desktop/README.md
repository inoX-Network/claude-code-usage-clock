# usage-clock in the Claude desktop app

The desktop app does not show the `statusLine` from `settings.json`. This folder is a Claude Code plugin (a mod with
a hooks module) that draws the usage-clock line under the prompt instead:

```
5h 17% ↻12:50 | week 76% ↻Mon 15:00 <- weekly limit reached | ctx 340k/1000k 34% | cache ~54m | $3.50
```

![The line under the prompt of the desktop app, cut off after the week](../docs/images/desktop-footer.png)

The footer is shared with the permission mode and the model, so a narrow window cuts the line. Pointing at it shows
all of it:

![The whole line as a tooltip: 5h 24 %, week 77 % with the stop marker, ctx 760k/1000k 76 %, cache ~59m, $43.15](../docs/images/desktop-footer-hover.png)

## Install

```
claude plugin marketplace add inoX-Network/claude-code-usage-clock
claude plugin install usage-clock-display@usage-clock
```

Start a new session (or `/reload-plugins`). From a local checkout, `claude plugin marketplace add <path to the
checkout>` loads the plugin in place, so edits take effect at the next session start.

## What it shows

The parts, texts and colours of the [status line](../README.md#what-the-status-line-shows), with two differences:

- **Cache clock is an estimate.** A plugin learns when the last answer of the main session came, not when the prompt
  cache expires. The clock counts one hour (the cache of a Claude subscription) from that answer and marks the
  estimate with `~`. With an API key the cache lives five minutes, so the clock then runs too long.
- **Cost** of the session follows at the end, in grey.

The numbers come from Claude Code itself (`$.session.usage()`), not from `rate-limit.json`.

In the terminal the plugin draws nothing: the status line shows the same there.

## Configuration

The colours follow the `statusline` section of the usage-clock `config.json` (`yellow_from`, `red_from`,
`week_stop_marker`, `show_reset`, `context_yellow_from_k`, `context_red_from_k`, `cache_yellow_below_min`) and
`thresholds.start_block_week` for `<- weekly limit reached`. The file is read once per session start. A missing
file or a key with the wrong type means the default, as for the hooks.

The plugin option `config_file` names the file (default `~/.claude/hooks/usage-clock/config.json`, where
`tools/install.py` puts it). Change it under `/config` or with
`claude plugin install usage-clock-display@usage-clock --config config_file=<path>`.

## Tests

```
claude plugin validate desktop
claude plugin test desktop
```

`tsconfig.json` extends the types Claude Code writes beside the manifest (`.claude-plugin/types/`) the first time
it loads the plugin from a folder; until then an editor shows the `claude-code` imports as unresolved.
