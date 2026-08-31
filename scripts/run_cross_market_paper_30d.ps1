[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repoRoot ".venv-vnpy\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "vn.py virtual-environment Python was not found: $python"
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $ownerIds = @($listener | Select-Object -ExpandProperty OwningProcess -Unique)
    $owners = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $ownerIds -contains $_.ProcessId }
    )
    $expectedOwner = $owners | Where-Object {
        $_.CommandLine -match "uvicorn" -and
        $_.CommandLine -match "server:app" -and
        $_.CommandLine -match "--port\s+$Port(?:\s|$)"
    }
    if ($expectedOwner) {
        Write-Output "Cross-market paper service already listens on 127.0.0.1:$Port."
        exit 0
    }
    throw "Port $Port is already owned by an unexpected process."
}

Set-Location -LiteralPath $repoRoot
& $python -m uvicorn server:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
