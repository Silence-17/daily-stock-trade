param(
    [string]$PythonExecutable = "python",
    [string]$VenvPath = ".venv-vnpy",
    [switch]$AllowUntestedPython,
    [switch]$SkipProjectDependencies,
    [string]$AlphaSiftSource = ""
)

$ErrorActionPreference = "Stop"

$versionJson = & $PythonExecutable -c "import json,sys; print(json.dumps({'major':sys.version_info.major,'minor':sys.version_info.minor,'version':sys.version.split()[0]}))"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to execute Python: $PythonExecutable"
}
$version = $versionJson | ConvertFrom-Json
if ($version.major -ne 3 -or $version.minor -lt 10) {
    throw "vn.py 4.4.0 requires Python 3.10 or newer; found $($version.version)."
}
if ($version.minor -gt 13 -and -not $AllowUntestedPython) {
    throw "Python $($version.version) is newer than vn.py 4.4.0's published 3.10-3.13 classifiers. Use Python 3.13, or pass -AllowUntestedPython explicitly."
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$target = [System.IO.Path]::GetFullPath((Join-Path $root $VenvPath))
$rootPrefix = $root.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $target.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "VenvPath must resolve inside the repository: $target"
}

Write-Host "Creating vn.py runtime environment at $target with Python $($version.version)"
& $PythonExecutable -m venv $target
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create vn.py virtual environment."
}

$venvPython = Join-Path $target "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $venvPython = Join-Path $target "bin/python"
}
if (-not (Test-Path $venvPython)) {
    throw "Virtual environment Python was not found under $target."
}

$env:PIP_CACHE_DIR = Join-Path $target ".pip-cache"
New-Item -ItemType Directory -Force -Path $env:PIP_CACHE_DIR | Out-Null
$pipNetworkArgs = @("--timeout", "20", "--retries", "2", "--progress-bar", "off")

& $venvPython -m pip install @pipNetworkArgs --upgrade pip wheel
if ($LASTEXITCODE -ne 0) {
    throw "Failed to upgrade pip and wheel."
}
if (-not $SkipProjectDependencies) {
    $projectRequirements = Join-Path $root "requirements.txt"
    if ($AlphaSiftSource) {
        $alphaSiftPath = [System.IO.Path]::GetFullPath((Join-Path $root $AlphaSiftSource))
        if (-not (Test-Path $alphaSiftPath)) {
            throw "AlphaSiftSource does not exist: $alphaSiftPath"
        }
        $projectRequirements = Join-Path $target "requirements-without-alphasift.txt"
        Get-Content -Encoding UTF8 (Join-Path $root "requirements.txt") |
            Where-Object { $_ -notmatch '^git\+https://github\.com/ZhuLinsen/alphasift\.git' } |
            Set-Content -Encoding UTF8 $projectRequirements
    }

    # LiteLLM publishes Windows wheels, while its newest source release may
    # trigger an unnecessary Rust toolchain build during dependency solving.
    & $venvPython -m pip install @pipNetworkArgs --only-binary=:all: "litellm>=1.80.10,!=1.82.7,!=1.82.8,<2.0.0"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install a compatible LiteLLM wheel for the vn.py runtime environment."
    }

    & $venvPython -m pip install @pipNetworkArgs --prefer-binary -r $projectRequirements
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install project dependencies. Re-run with -SkipProjectDependencies only for an adapter-only diagnostic environment."
    }
    if ($AlphaSiftSource) {
        & $venvPython -m pip install @pipNetworkArgs $alphaSiftPath
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install AlphaSift from $alphaSiftPath."
        }
    }
}
& $venvPython -m pip install @pipNetworkArgs --extra-index-url https://pypi.vnpy.com -r (Join-Path $root "requirements-vnpy.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install optional vn.py dependencies."
}
& $venvPython (Join-Path $root "scripts\check_vnpy_adapter.py") --require-vnpy --reconnect-cycles 3
if ($LASTEXITCODE -ne 0) {
    throw "vn.py runtime smoke check failed."
}

Write-Host "vn.py runtime environment is ready: $venvPython"
