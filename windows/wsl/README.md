# usage-clock in WSL

Claude Code inside WSL is a Linux program with a Linux Python: usage-clock runs there exactly as on Linux, installed
with `python3 tools/install.py`. There is nothing to port; this folder only holds the proof.

`run-suite.ps1` (from Windows PowerShell) or `run-suite.sh` (from a WSL shell) runs the main suite `tests/` under
Linux, on the checkout as it is, and applies the CI rule to the result: no empty run and not a single skipped test.

```powershell
powershell -ExecutionPolicy Bypass -File windows\wsl\run-suite.ps1                 # distribution "Ubuntu"
powershell -ExecutionPolicy Bypass -File windows\wsl\run-suite.ps1 -Distro Debian -Venv /home/me/venvs/uc
powershell -ExecutionPolicy Bypass -File windows\wsl\run-suite.ps1 -k statusline   # extra arguments go to pytest
wsl -d Ubuntu -- bash windows/wsl/run-suite.sh                                      # the same without PowerShell
```

`-ExecutionPolicy Bypass` is for this one call only, on machines that do not allow PowerShell scripts; it changes no
setting. Run it from the checkout (`wsl` starts in the current Windows folder, translated to `/mnt/c/...`).

The first run makes `~/.cache/usage-clock-wsl/venv` in the distribution and installs `requirements-dev.txt` into it
(pip, from the internet); the distribution needs `python3` with the `venv` module. `-Venv` / `USAGE_CLOCK_WSL_VENV`
points to a virtual environment that already has pytest. Nothing is written into the checkout (no `.pyc`, no pytest
cache). Reading the checkout through `/mnt/c` is slower than a clone inside the Linux file system, but the result is
the same.

Two things differ from a real Linux machine: the checkout sits on a Windows file system (the test files are
created in `/tmp`, so the tests do not notice), and Claude Code in WSL keeps its settings in the home folder of the
distribution (`~/.claude`), not in your Windows profile.
