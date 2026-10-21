# Getting started

This guide is for you if you use Claude Code and have never installed anything like this before. You need no
programming knowledge. Every step says what to type and what you should see afterwards. The
[documentation](README.md) has all the details, but you do not need it to get started.

## What it does

Claude Code has a limit on how much you can use it in five hours and in a week. This tool shows you how much of that
you have used, at the bottom of the Claude Code window, and tells Claude the same numbers with every message. It can
also stop Claude from starting many helpers at once when you are close to your limit, but at first it only watches
and tells you what it would have done.

## Open a terminal

Everything in this guide is typed into a *terminal*: a window where you type a command, press Enter, and read the
answer below it.

| System | How to open it | Where you are when it opens |
|---|---|---|
| Linux | Press Ctrl+Alt+T, or search your apps for *Terminal* (on KDE: *Konsole*) | Your home folder, for example `/home/anna`; the prompt shows it as `~` |
| macOS | Press Cmd+Space, type `Terminal`, press Enter | Your home folder, for example `/Users/anna`; the prompt shows it as `~` |
| Windows | Press the Windows key, type `powershell`, press Enter | Your home folder, for example `C:\Users\Anna`; the prompt reads `PS C:\Users\Anna>` |

To see at any time which folder you are in, type `pwd` and press Enter (this works in all three).

Use **one terminal window for the whole guide** and type the commands **one after the other**, each followed by
Enter. Wait until a command has finished (the prompt appears again) before you type the next one. A later step
counts on the folder an earlier step went into: if you close the window, open a new one and go into the folder
again with `cd claude-code-usage-clock`.

To paste a command instead of typing it: Ctrl+Shift+V on Linux, Cmd+V on macOS, Ctrl+V or a right click in
PowerShell. The command boxes in this guide hold nothing but the command, so you can copy a box as a whole.

## Before you start

You need:

- **Claude Code with a Pro or Max subscription.** With an API key (or through Bedrock or Vertex) the usage numbers
  are not available, and the tool shows `unknown` instead.
- **Python 3.10 or newer.** Python is the program that runs the tool.
- **Git.** Git downloads the tool. On Windows, Claude Code also needs it to run the tool.

To check what you have, type the commands for your system into the terminal, one at a time.

### Linux

```sh
python3 --version
```

```sh
git --version
```

You should see something like `Python 3.12.3` and `git version 2.43.0`. The Python number must be 3.10 or higher.
If a command says `command not found`, install what is missing with your system's package manager, for example on
Ubuntu or Debian:

```sh
sudo apt install python3 git
```

### macOS

```sh
python3 --version
```

```sh
git --version
```

You should see something like `Python 3.12.3` and `git version 2.39.3`. The Python that comes with macOS is 3.9,
which is too old. If you see `Python 3.9` or lower, install a current one from
[python.org](https://www.python.org/downloads/macos/), then close the terminal, open a new one and check again. If
macOS offers to install the *command line developer tools* when you type `git`, accept: that installs Git.

### Windows

```powershell
python --version
```

```powershell
git --version
```

You should see something like `Python 3.13.1` and `git version 2.47.1.windows.1`.

- If `python` opens the Microsoft Store or says `Python was not found`, install Python from
  [python.org](https://www.python.org/downloads/windows/). In the installer, tick **Add python.exe to PATH**.
- If `git` says it is not recognized, install [Git for Windows](https://git-scm.com/downloads/win) and keep the
  default settings.
- Then close PowerShell, open a new one and check again.

On Windows the command is `python`, not `python3`: the `python3` of Windows is only a shortcut to the Microsoft Store.

## Install

Pick a folder where the tool may stay, for example your home folder (the terminal starts there). The folder is only
used for updates and reports; Claude Code runs a copy elsewhere.

**Step 1: download the tool.**

```sh
git clone https://github.com/inoX-Network/claude-code-usage-clock
```

You should see a few lines that end with `Resolving deltas: 100% ... done.` A new folder `claude-code-usage-clock`
now exists.

**Step 2: go into that folder.**

```sh
cd claude-code-usage-clock
```

You see no output; that is fine.

**Step 3: start the installer.**

On Linux and macOS:

```sh
python3 tools/install.py
```

On Windows:

```powershell
python tools\install.py
```

It asks one question per part of the tool, each with a line above it that says what the part does:

```
Shows the model, the 5-hour and weekly usage and the context below the prompt, and measures the usage the other parts read.
Step 1/8 - Status line: model, 5h/week usage and context below the prompt? [Y/n] (default)
```

Press Enter to take the answer in brackets (the capital letter), or type `y` or `n` and press Enter. After the
brackets stands `(default)`: on a first install every answer in brackets is the safe start. Not sure what a part
does? Type `h` or `i` and press Enter: a few lines explain it, then the same question comes again (`help` and
`--help` work too; at the repository and the name, type `?`). For a start,
Enter at every question is a good choice: all five parts, the gates only watch (*shadow mode*, see below), no
automatic continue and no sync. If you already have a status line of your own, the installer also asks whether to
replace it; with *no*, yours stays (see [The status line shows nothing](#the-status-line-shows-nothing)). To stop
without any change, press Ctrl+C.

**Step 4: check the summary and install.**

After the last question you see what you chose, and nothing has been written yet:

```
Summary:
  Status line: yes
  Usage clock: yes
  Context reporter: yes
  Spawn gate: yes
  Soft stop: yes
  Gates: shadow, they only log
  Auto-continue: no
  Sync: no, sync.mode off
Same as: python3 tools/install.py --components statusline,usage-clock,context-reporter,spawn-gate,soft-stop --mode shadow --sync-mode off

Install now? [Y/n]
```

The *Same as* line is the same installation as one command, for later or for a script. Press Enter. The installer
first tries each part once and then writes; near the end you see:

```
Smoke test: passed (statusline, usage-clock, context-reporter, spawn-gate, soft-stop).
...
Written: ...settings.json
Backup:  ...settings.json.bak-usage-clock-...
Undo:    cp "..." "..."
Start a new Claude Code session for the changes to take effect.
Mode shadow: nothing is blocked, decisions are only logged. Use --mode enforce to switch them on.
```

*Smoke test: passed* means the tool tried each of its parts once and they all work on your computer. If you see an
error instead, look at [Common problems](#common-problems). The installer changed one file, the settings of Claude
Code, and kept a copy of the old one (*Backup*; there is none if you had no settings yet). The *Undo* line is the
command that puts the old file back, should you ever want that. The last line says where the finer settings are.

Run the installer again later and it asks the same questions, with what you have now as the answers in brackets,
marked `(current)`: Enter at every question changes nothing. Want to see what would change without any question? Add `--dry-run`.

**Step 5: restart Claude Code.** Close every Claude Code window and start it again. The tool is only loaded when
Claude Code starts.

**Step 6: check that it worked.** In the same terminal as before:

On Linux and macOS:

```sh
python3 tools/install.py --check
```

On Windows:

```powershell
python tools\install.py --check
```

This changes nothing. Every line starts with `OK`, `FIX` or `NOTE`, and the last line should read:

```
Check: no problems.
```

A `FIX` line says what is wrong and what to do; most often it is "Run install.py again". A `NOTE` is only worth
knowing: right after installing, `NOTE Usage: no measurement yet` is normal, because Claude has not answered yet.
Run the check again after Claude's first answer, and that line becomes an `OK` line ("measured 0 min ago").

Run the check after every update (`git pull`, then the installer) too: it tells you if the installed copy is older
than the downloaded one.

## What you see now

At the bottom of the Claude Code window there is a new line, the *status line*:

![The status line: model, 5h usage with reset time, weekly usage, context size and prompt cache clock](images/statusline.png)

From left to right:

- **Opus** is the model you are talking to.
- **5h 57%** means you have used 57 % of your five-hour limit; **↻22:40** is when it starts again from zero.
- **week 23%** is your weekly limit.
- **ctx 485k/1000k** is how much the conversation holds: the longer it gets, the more every message costs.
- **cache 58m** is how long Claude still remembers this conversation cheaply. After that, the next message costs
  more.

Grey is fine, yellow means "keep an eye on it", red means "close to the limit" (for the usage: yellow from 50 %, red
from 75 %).

Right after the restart the line is shorter: the usage numbers appear after Claude's **first answer**, because
Claude Code only passes them on then. Send any message, wait for the answer, and the full line appears.

You do not see the second part of the tool: with every message, Claude now also gets a line like
`Usage: 5h 40 % (resets 14:00) · week 61 % ... · Now: Wed 14 Jan 2026 10:05`. Ask Claude "What does your usage line
say?" and it will tell you.

Using the Claude desktop app instead of the terminal? The app does not show status lines; a small plugin draws the
same line there, see [`desktop/`](../desktop/README.md).

## Shadow mode: why nothing is blocked yet

Two parts of the tool, the *gates*, can step in near your limit: one stops Claude from starting new helpers
(*agents*), the other slows helpers down that are already running. A new install starts them in **shadow mode**:
they decide what they would do, write it into a log, and Claude tells you ("in enforce mode this start would have
been denied ..."), but nothing is actually stopped.

This is on purpose. Every way of working is different, and you should see what the gates would do before you let
them do it. Use the tool for a few days. If the notes make sense to you, switch the gates on (*enforce mode*) by
running the installer once more:

```sh
python3 tools/install.py --mode enforce
```

(On Windows: `python tools\install.py --mode enforce`.) To go back, run it with `--mode shadow`. The README explains
what to look for in the log: [From shadow to enforce](gates.md#from-shadow-to-enforce).

## Optional: several computers in one report

Skip this if you use Claude Code on one computer only. Your limits count for all your computers together, and each
computer only knows its own usage. The *sync* lets every computer put its numbers into a private GitHub repository of
yours, so that the report on each one shows all of them. It is off until you install it.

1. On GitHub, create a new **private** repository, for example `usage-sync`.
2. Let `git` on this computer sign in to GitHub once; the tool itself never asks for a password. On Windows, type
   `git ls-remote https://github.com/anna/usage-sync.git` (with your GitHub name instead of `anna`) and sign in in the
   browser window that Git for Windows opens. On Linux and macOS the simplest way is the
   [GitHub CLI](https://cli.github.com/): `gh auth login`, then `gh auth setup-git`. Both give git access to all
   your repositories; the safer way, a key or token for this one repository only, is on
   [the sync page](sync.md#install).
3. In the folder from step 2 of the install, with your GitHub name instead of `anna`:

```sh
python3 tools/install.py --components sync --sync-repo anna/usage-sync
```

(On Windows: `python tools\install.py --components sync --sync-repo anna/usage-sync`.) Or run the installer without
options and answer `y` at the sync question, then give the name of the repository and a name for this computer
(Enter takes `linux`, `darwin` or `windows`; your computer's own name is never used): it does the same. The installer first checks that
nobody can read the repository without logging in. If it is public, it stops and changes nothing in your settings. Then it starts the
sync in shadow mode: it prepares everything on this computer but sends nothing yet. Do the same on your other
computers, and switch it on everywhere once you are happy:

```sh
python3 tools/install.py --components sync --sync-mode on
```

What it sends, and what it never sends: [The sync across machines](sync.md).

## Uninstall

Go into the folder from step 2 and run:

```sh
python3 tools/install.py --uninstall
```

(On Windows: `python tools\install.py --uninstall`.) This takes the tool out of the Claude Code settings, again with
a backup. Restart Claude Code, and the status line is gone.

The files stay on your disk. To remove them as well, delete these three folders and the one file:

| What | Linux and macOS | Windows (PowerShell) |
|---|---|---|
| The tool's copy | `rm -r ~/.claude/hooks/usage-clock` | `Remove-Item -Recurse "$env:USERPROFILE\.claude\hooks\usage-clock"` |
| Its logs | `rm -r ~/.cache/claude-usage-clock` | `Remove-Item -Recurse "$env:USERPROFILE\.cache\claude-usage-clock"` |
| The shared usage numbers | `rm ~/.claude/rate-limit.json` | `Remove-Item "$env:USERPROFILE\.claude\rate-limit.json"` |
| The downloaded folder | `rm -r ~/claude-code-usage-clock` | `Remove-Item -Recurse -Force "$env:USERPROFILE\claude-code-usage-clock"` (`-Force`: Git marks some of its files read-only) |

(`~` is your home folder.) If you had a status line of your own before and replaced it, the README says how to get
it back: [Uninstall completely](install.md#uninstall-completely).

## Common problems

### `python3: command not found`

- **Windows:** type `python` instead of `python3` (or `py -3`). If that does not work either, Python is not
  installed or not on the PATH: see [Windows](#windows) above.
- **macOS and Linux:** Python is not installed. See [macOS](#macos) or [Linux](#linux) above.

### The status line shows nothing

- **You have not restarted Claude Code yet**, or an old window is still open. Close every Claude Code window and
  start it again.
- **No answer yet.** Before Claude's first answer the usage parts are missing. Send a message and wait.
- **You had a status line of your own.** The installer leaves it alone and says so (`LEFT UNCHANGED`). To use this
  one instead, run the installer with `--replace-statusline`. Your old one is in the backup the installer names.
- **You use the Claude desktop app.** It shows no status lines; use the plugin in [`desktop/`](../desktop/README.md).
- **Windows without Git for Windows.** The installer stops with `Error: Git Bash was not found`. Install
  [Git for Windows](https://git-scm.com/downloads/win) and run the installer again.

### Usage shows `unknown`

- **No answer yet in this session.** The numbers arrive with Claude's first answer.
- **No Pro or Max subscription.** With an API key, Bedrock or Vertex, Claude Code gets no usage numbers, so there is
  nothing to show.
- **Another status line is running** (see above). Only this tool's status line writes the numbers that the rest of
  the tool reads.
- **You work in the Claude desktop app.** The app never runs the status line. Install the
  [desktop plugin](../desktop/README.md): after each answer it hands the app's numbers to the status line, which then
  writes them as in the terminal. It needs the status line from `tools/install.py` in `settings.json`, and a new
  session (or `/reload-plugins`) after installing or updating the plugin.

If it says `STALE` instead, the last measurement is old, for example because no session has answered for a while.
Send a message, and after the answer the numbers are fresh again.

## Next steps

- The [documentation](README.md) explains every part and every setting.
- [`docs/usage-report.md`](usage-report.md) shows how to see what your usage went into, per day, week, month or year;
  the [desktop plugin](../desktop/README.md#the-usage-report-report) shows the same in Claude Code with `/report`.
- On Windows, [`windows/README.md`](../windows/README.md) lists what is different there.
