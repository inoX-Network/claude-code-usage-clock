# Runs the main suite of this repository under Linux in WSL, from a Windows PowerShell:
#   powershell -ExecutionPolicy Bypass -File windows\wsl\run-suite.ps1 [-Distro Ubuntu] [-Venv <path in WSL>] [pytest arguments]
#
# The checkout stays where it is (on the Windows disk, reached as /mnt/c/...); windows/wsl/run-suite.sh does the work
# inside the distribution. Exit code: that of pytest, or 1 if the CI rule (no empty run, no skip) finds a problem.
# Everything that is not -Distro or -Venv goes to pytest. (No param block: it would take "tests/x.py" for a distribution.)
$Distro = 'Ubuntu'
$Venv = ''
$PytestArguments = @()
for ($i = 0; $i -lt $args.Count; $i++) {
    switch -Regex ($args[$i]) {
        '^-Distro$' { $Distro = $args[++$i] }
        '^-Venv$' { $Venv = $args[++$i] }
        default { $PytestArguments += $args[$i] }
    }
}
$ErrorActionPreference = 'Stop'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path -replace '\\', '/'
$script = (& wsl.exe -d $Distro -- wslpath -a "$repo/windows/wsl/run-suite.sh") | Out-String
$script = $script.Trim()
if ($LASTEXITCODE -ne 0 -or -not $script) {
    Write-Error "WSL distribution '$Distro' not found or wslpath failed. List them with: wsl -l -v"
    exit 1
}
if ($Venv) { $env:USAGE_CLOCK_WSL_VENV = $Venv; $env:WSLENV = 'USAGE_CLOCK_WSL_VENV' }
& wsl.exe -d $Distro -- bash $script @PytestArguments
exit $LASTEXITCODE
