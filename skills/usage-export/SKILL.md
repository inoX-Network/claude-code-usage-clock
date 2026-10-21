---
name: usage-export
description: Puts the token usage of this chat of Claude Code on the web (claude.ai) into the private sync repository of the usage clock - numbers only, never the chat. Use when the user types /usage-export (optionally "on"), as the last step of a chat.
---

# /usage-export

Every chat of Claude Code on the web runs in a VM of its own, without the hooks of the usage clock. This skill puts
the usage of this one session into the sync repository, so that the usage report at home shows it as the machine
`claude-web`. Run it as the last step of a chat: the VM may be gone later, and nothing runs by itself at its end.

## Settings of your copy

- Sync repository: `OWNER/NAME` (private; the same one the computers at home use)
- Usage clock: `https://github.com/inoX-Network/claude-code-usage-clock`, tag `v1.7.0` (the first release with
  `tools/web_export.py`; raise it for a newer release)

## Arguments

- none: shadow mode. Everything is checked and committed in the VM, nothing is pushed. The summary says how many
  lines would have gone out. Use it until a few chats looked right.
- `on`: the push goes out to the sync repository.

## Steps

1. Run `printenv CLAUDE_CODE_REMOTE`. Unless it prints `true`, stop and tell the user: this skill is for Claude Code
   on the web; on a computer with the hooks installed the sync exports by itself.
2. Attach the sync repository to this session with push access (the session's tool for adding a repository). If that
   is refused, stop and say which repository could not be attached.
3. Clone the usage clock at the tag of the settings into a new, empty folder in the scratchpad
   (`git clone --depth 1 --branch <tag> <url> <folder>`), then check that `git -C <folder> describe --tags
   --exact-match` prints exactly that tag. Anything else: stop, nothing is run.
4. Run `python3 -I <folder>/tools/web_export.py --sync-repo OWNER/NAME`, with `--sync-mode on` only if the user gave
   `on`. It installs the sync into this VM (name `claude-web`, a settings file of its own, so this session gets no
   hook), reads the numbers of the debug log, and starts the push of the sync once.
5. Give the user its summary as it is: the side calls, the outcome of the push, and what the repository holds of this
   session. Say that the request of this very step is not in it yet (the transcript gets it only after the answer).

## Never

- Never read the debug log, the transcript or the exported lines yourself, and never show their content: the script
  takes only numbers, times and source names from them.
- Never edit `visibility.json`, `config.json`, the clone or the log folder to get past a check. If the installer stops
  (a public repository, a visibility that cannot be told, a clone that fails), report its message and stop.
- Never push to the sync repository with git yourself: only the push of the sync writes there.
