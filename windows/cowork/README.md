# usage-clock in Cowork - findings, not promises

No code lives here and nothing is supported. This records what is known about Cowork, so that nobody builds on a guess.

- Cowork runs in the cloud by default. Run locally, it runs in a Linux virtual machine (Hyper-V on Windows), not in
  the Windows terminal that [`../cli/`](../cli/) is for.
- It loads the hooks of a plugin (`hooks/hooks.json`).
- It ignores the settings of a plugin. That means **no status line**: the status line is where the usage numbers come
  from (`rate_limits` in its input), and every part of usage-clock that reads them needs it.
- A plugin has no `bin/` there.
- Plugins come from the claude.ai account, not from a local folder, so `tools/install.py` has nothing to install into.
- **Not established:** whether `rate_limits` reaches a Cowork session at all. It would have to be measured with a test
  hook that writes the input it receives to a file; until someone does that, the usage file would stay empty and every
  hook would report the usage as `unknown`.
