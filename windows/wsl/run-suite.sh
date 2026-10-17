#!/usr/bin/env bash
# Runs the main suite (tests/) of this repository under Linux, inside WSL. Called by run-suite.ps1, or by hand from a
# WSL shell:  bash windows/wsl/run-suite.sh [extra pytest arguments]
#
# Needs python3 with the venv module in the distribution. The first run makes a virtual environment in
# ~/.cache/usage-clock-wsl/venv and installs requirements-dev.txt into it (pip, from the internet); set
# USAGE_CLOCK_WSL_VENV to use another one that has pytest. Nothing is written into the checkout: no .pyc, no pytest cache.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
venv="${USAGE_CLOCK_WSL_VENV:-$HOME/.cache/usage-clock-wsl/venv}"

if [ ! -x "$venv/bin/python" ]; then
    echo "Making the virtual environment $venv (first run only)"
    python3 -m venv "$venv"
    "$venv/bin/python" -m pip install --quiet -r "$repo/requirements-dev.txt"
fi

cd "$repo"
export PYTHONDONTWRITEBYTECODE=1
output="$(mktemp)"
trap 'rm -f "$output"' EXIT
set +e
"$venv/bin/python" -m pytest -q -p no:cacheprovider -rs tests/ "$@" | tee "$output"
status=${PIPESTATUS[0]}
set -e
# The same rule as the CI: no empty run, no skip at all (everything runs under Linux)
"$venv/bin/python" .github/check_pytest_output.py "$output" || [ "$status" -ne 0 ] || status=1
exit "$status"
