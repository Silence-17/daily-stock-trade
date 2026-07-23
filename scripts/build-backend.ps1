param(
  [switch]$IncludeVnpy,
  [switch]$SkipDependencyInstall,
  [string]$VnpyGatewayPluginsJson = $env:VNPY_GATEWAY_PLUGINS_JSON
)

$ErrorActionPreference = 'Stop'
$includeVnpyRuntime = $IncludeVnpy -or ($env:DSA_INCLUDE_VNPY_DESKTOP -eq 'true')
$gatewayPluginsJson = if ([string]::IsNullOrWhiteSpace($VnpyGatewayPluginsJson)) { '[]' } else { $VnpyGatewayPluginsJson }
$gatewayPluginModules = @()

Write-Host 'Building React UI (static assets)...'
Push-Location 'apps\dsa-web'
if (!(Test-Path 'node_modules')) {
  npm install
}
npm run build
Pop-Location

$pythonBin = $env:PYTHON_BIN
if ([string]::IsNullOrWhiteSpace($pythonBin)) {
  $pythonBin = 'python'
}

Write-Host "Using Python: $pythonBin"

Write-Host 'Verifying static asset references (source)...'
& $pythonBin "${PSScriptRoot}\check_static_assets.py" 'static'
if ($LASTEXITCODE -ne 0) {
  throw "Static asset sanity check failed for source static/. See GitHub #1064."
}

function Test-PythonCode {
  param(
    [string]$Python,
    [string]$Code
  )

  try {
    & $Python -c $Code *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

Write-Host 'Building backend executable...'
if (-not (Test-PythonCode -Python $pythonBin -Code "import PyInstaller")) {
  & $pythonBin -m pip install pyinstaller
}

if (-not $SkipDependencyInstall) {
  Write-Host 'Installing backend dependencies...'
  & $pythonBin -m pip install -r requirements.txt
  if ($LASTEXITCODE -ne 0) {
    throw "pip install -r requirements.txt failed with exit code $LASTEXITCODE."
  }
} else {
  Write-Host 'Skipping backend dependency installation; import checks remain enabled.'
}

Write-Host 'Checking python-multipart availability...'
if (-not (Test-PythonCode -Python $pythonBin -Code "import multipart, multipart.multipart")) {
  throw 'python-multipart is not importable in the selected Python environment.'
}

Write-Host 'Checking AlphaSift adapter availability...'
if (-not (Test-PythonCode -Python $pythonBin -Code "import alphasift.dsa_adapter")) {
  throw 'alphasift.dsa_adapter is not importable after installing requirements.'
}

function Invoke-VnpyGatewayPluginHelper {
  param([string[]]$Arguments)

  $previousManifest = $env:VNPY_GATEWAY_PLUGINS_JSON
  try {
    $env:VNPY_GATEWAY_PLUGINS_JSON = $gatewayPluginsJson
    $output = & $pythonBin "${PSScriptRoot}\vnpy_gateway_plugins.py" @Arguments
    if ($LASTEXITCODE -ne 0) {
      throw "vn.py gateway plugin helper failed with exit code $LASTEXITCODE."
    }
    return $output
  } finally {
    if ($null -eq $previousManifest) {
      Remove-Item Env:VNPY_GATEWAY_PLUGINS_JSON -ErrorAction SilentlyContinue
    } else {
      $env:VNPY_GATEWAY_PLUGINS_JSON = $previousManifest
    }
  }
}

if ($includeVnpyRuntime) {
  Write-Host 'Installing optional vn.py desktop runtime...'
  $versionJson = & $pythonBin -c "import json,sys; print(json.dumps({'major':sys.version_info.major,'minor':sys.version_info.minor,'version':sys.version.split()[0]}))"
  $version = $versionJson | ConvertFrom-Json
  if ($version.major -ne 3 -or $version.minor -lt 10 -or $version.minor -gt 13) {
    throw "The vn.py desktop bundle requires Python 3.10-3.13; found $($version.version)."
  }
  if (-not $SkipDependencyInstall) {
    & $pythonBin -m pip install --prefer-binary --extra-index-url https://pypi.vnpy.com -r requirements-vnpy.txt
    if ($LASTEXITCODE -ne 0) {
      throw "pip install -r requirements-vnpy.txt failed with exit code $LASTEXITCODE."
    }
  }
  $pluginArgs = @()
  if (-not $SkipDependencyInstall) {
    $pluginArgs += '--install'
  }
  $pluginArgs += '--verify'
  Invoke-VnpyGatewayPluginHelper -Arguments $pluginArgs | Write-Host
  $gatewayModulesJson = Invoke-VnpyGatewayPluginHelper -Arguments @('--print-modules-json')
  foreach ($module in ($gatewayModulesJson | ConvertFrom-Json)) {
    $gatewayPluginModules += [string]$module
  }
  if (-not (Test-PythonCode -Python $pythonBin -Code "import vnpy, vnpy.event, vnpy.trader.engine, vnpy.trader.event, vnpy.trader.object")) {
    throw 'vn.py core runtime is not importable in the selected Python environment.'
  }
} else {
  Invoke-VnpyGatewayPluginHelper -Arguments @('--assert-empty') | Write-Host
}

if (Test-Path 'dist\backend') {
  Remove-Item -Recurse -Force 'dist\backend'
}
New-Item -ItemType Directory -Path 'dist\backend' | Out-Null

if (Test-Path 'dist\stock_analysis') {
  Remove-Item -Recurse -Force 'dist\stock_analysis'
}

if (Test-Path 'build\stock_analysis') {
  Remove-Item -Recurse -Force 'build\stock_analysis'
}

$hiddenImports = @(
  'multipart',
  'multipart.multipart',
  'json_repair',
  'tiktoken',
  'tiktoken_ext',
  'tiktoken_ext.openai_public',
  'api',
  'api.app',
  'api.deps',
  'api.v1',
  'api.v1.router',
  'api.v1.endpoints',
  'api.v1.endpoints.analysis',
  'api.v1.endpoints.history',
  'api.v1.endpoints.stocks',
  'api.v1.endpoints.health',
  'api.v1.endpoints.alphasift',
  'alphasift',
  'alphasift.dsa_adapter',
  'api.v1.schemas',
  'api.v1.schemas.analysis',
  'api.v1.schemas.history',
  'api.v1.schemas.stocks',
  'api.v1.schemas.common',
  'api.middlewares',
  'api.middlewares.error_handler',
  'src.services',
  'src.services.task_queue',
  'src.services.analysis_service',
  'src.services.history_service',
  'src.services.alphasift_service',
  'src.services.vnpy_adapter',
  'src.services.vnpy_runtime',
  'src.services.vnpy_simulated_gateway',
  'uvicorn.logging',
  'uvicorn.loops',
  'uvicorn.loops.auto',
  'uvicorn.protocols',
  'uvicorn.protocols.http',
  'uvicorn.protocols.http.auto',
  'uvicorn.protocols.websockets',
  'uvicorn.protocols.websockets.auto',
  'uvicorn.lifespan',
  'uvicorn.lifespan.on'
)
$hiddenImportArgs = $hiddenImports | ForEach-Object { "--hidden-import=$_" }

$pyInstallerArgs = @(
  '-m', 'PyInstaller',
  '--name', 'stock_analysis',
  '--onedir',
  '--noconfirm',
  '--noconsole',
  '--add-data', 'static;static',
  '--add-data', 'strategies;strategies',
  '--collect-data', 'litellm',
  '--collect-data', 'tiktoken',
  '--collect-all', 'alphasift'
)
$pyInstallerArgs += $hiddenImportArgs
if ($includeVnpyRuntime) {
  $pyInstallerArgs += @('--collect-all', 'vnpy', '--collect-all', 'talib')
  foreach ($module in $gatewayPluginModules) {
    $pyInstallerArgs += @('--collect-all', $module)
  }
}
$pyInstallerArgs += 'main.py'

Write-Host "Running: $pythonBin $($pyInstallerArgs -join ' ')"
& $pythonBin @pyInstallerArgs
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed with exit code $LASTEXITCODE."
}

if (!(Test-Path 'dist\stock_analysis')) {
  throw 'PyInstaller finished but dist\stock_analysis was not generated.'
}

Copy-Item -Path 'dist\stock_analysis' -Destination 'dist\backend\stock_analysis' -Recurse -Force

Write-Host 'Verifying packaged AlphaSift importability...'
$packagedEntry = Join-Path 'dist\backend\stock_analysis' 'stock_analysis.exe'
if (-not (Test-Path $packagedEntry)) {
  throw "Packaged backend entrypoint not found: $packagedEntry"
}
$previousProbe = $env:DSA_PACKAGED_ALPHASIFT_IMPORT_PROBE
try {
  $env:DSA_PACKAGED_ALPHASIFT_IMPORT_PROBE = '1'
  $probeProcess = Start-Process -FilePath $packagedEntry -Wait -PassThru
  if ($probeProcess.ExitCode -ne 0) {
    throw "Packaged backend cannot import alphasift.dsa_adapter; probe exited with code $($probeProcess.ExitCode)."
  }
} finally {
  if ($null -eq $previousProbe) {
    Remove-Item Env:DSA_PACKAGED_ALPHASIFT_IMPORT_PROBE -ErrorAction SilentlyContinue
  } else {
    $env:DSA_PACKAGED_ALPHASIFT_IMPORT_PROBE = $previousProbe
  }
}

if ($includeVnpyRuntime) {
  Write-Host 'Verifying packaged vn.py runtime importability...'
  $previousVnpyProbe = $env:DSA_PACKAGED_VNPY_IMPORT_PROBE
  $previousPluginModules = $env:DSA_PACKAGED_VNPY_PLUGIN_MODULES_JSON
  $vnpyProbeStdout = Join-Path $env:TEMP 'dsa-packaged-vnpy-probe.stdout.log'
  $vnpyProbeStderr = Join-Path $env:TEMP 'dsa-packaged-vnpy-probe.stderr.log'
  try {
    $env:DSA_PACKAGED_VNPY_IMPORT_PROBE = '1'
    $env:DSA_PACKAGED_VNPY_PLUGIN_MODULES_JSON = ConvertTo-Json -InputObject @($gatewayPluginModules) -Compress
    $vnpyProbeProcess = Start-Process -FilePath $packagedEntry -Wait -PassThru `
      -RedirectStandardOutput $vnpyProbeStdout -RedirectStandardError $vnpyProbeStderr
    if ($vnpyProbeProcess.ExitCode -ne 0) {
      Get-Content $vnpyProbeStdout -ErrorAction SilentlyContinue | Write-Host
      Get-Content $vnpyProbeStderr -ErrorAction SilentlyContinue | Write-Host
      throw "Packaged backend cannot import the vn.py runtime; probe exited with code $($vnpyProbeProcess.ExitCode)."
    }
  } finally {
    if ($null -eq $previousVnpyProbe) {
      Remove-Item Env:DSA_PACKAGED_VNPY_IMPORT_PROBE -ErrorAction SilentlyContinue
    } else {
      $env:DSA_PACKAGED_VNPY_IMPORT_PROBE = $previousVnpyProbe
    }
    if ($null -eq $previousPluginModules) {
      Remove-Item Env:DSA_PACKAGED_VNPY_PLUGIN_MODULES_JSON -ErrorAction SilentlyContinue
    } else {
      $env:DSA_PACKAGED_VNPY_PLUGIN_MODULES_JSON = $previousPluginModules
    }
  }
}

Write-Host 'Verifying static asset references (packaged)...'
$packagedStatic = Join-Path 'dist\backend\stock_analysis' '_internal\static'
if (-not (Test-Path $packagedStatic)) {
  $packagedStatic = Join-Path 'dist\backend\stock_analysis' 'static'
}
if (Test-Path $packagedStatic) {
  & $pythonBin "${PSScriptRoot}\check_static_assets.py" $packagedStatic
  if ($LASTEXITCODE -ne 0) {
    throw "Static asset sanity check failed for packaged $packagedStatic. See GitHub #1064."
  }
} else {
  Write-Warning "Could not locate packaged static directory under dist\backend\stock_analysis; skipping post-package check."
}

Write-Host 'Verifying packaged built-in strategies...'
$sourceStrategyCount = @(Get-ChildItem -Path 'strategies' -Filter '*.yaml' -File).Count
$packagedStrategies = Join-Path 'dist\backend\stock_analysis' '_internal\strategies'
if (-not (Test-Path $packagedStrategies)) {
  $packagedStrategies = Join-Path 'dist\backend\stock_analysis' 'strategies'
}
if (-not (Test-Path $packagedStrategies)) {
  throw 'Packaged strategies directory not found under dist\backend\stock_analysis.'
}
$packagedStrategyCount = @(Get-ChildItem -Path $packagedStrategies -Filter '*.yaml' -File).Count
if ($packagedStrategyCount -ne $sourceStrategyCount) {
  throw "Packaged strategies count mismatch: expected $sourceStrategyCount, got $packagedStrategyCount."
}

Write-Host 'Backend build completed.'
