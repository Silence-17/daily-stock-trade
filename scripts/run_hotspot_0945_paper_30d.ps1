[CmdletBinding()]
param(
    [ValidateSet("Auto", "Open", "Close", "Status")]
    [string]$Phase = "Auto"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonCandidates = @(
    (Join-Path $repoRoot ".venv-vnpy\Scripts\python.exe"),
    (Join-Path $repoRoot ".venv\Scripts\python.exe")
)
$python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $python) {
    throw "No project Python runtime was found in .venv-vnpy or .venv."
}

Set-Location -LiteralPath $repoRoot
& $python (Join-Path $PSScriptRoot "run_hotspot_0945_paper_30d.py") --phase $Phase.ToLowerInvariant()
exit $LASTEXITCODE

