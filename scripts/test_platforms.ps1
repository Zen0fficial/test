param(
    [Parameter(Mandatory = $true)][string]$CapturePath,
    [string]$OutputRoot = ('output/platform_compatibility/run-' + (Get-Date -Format 'yyyyMMdd-HHmmss')),
    [string]$QlibPython = '.venv-qlib/Scripts/python.exe',
    [string]$NautilusPython = '.venv-nautilus/Scripts/python.exe',
    [string]$DotNet = 'tmp/dotnet/dotnet.exe',
    [string]$QlibSource = 'tmp/qlib-v0.9.7',
    [string]$LeanSource = 'tmp/lean-src'
)
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location $repo
try {
    foreach ($required in @($CapturePath, $QlibPython, $NautilusPython, $DotNet, $QlibSource, $LeanSource)) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Missing prerequisite: $required. See docs/platform-converters.md." }
    }
    $capture = (Resolve-Path -LiteralPath $CapturePath).Path
    $qlib = (Resolve-Path -LiteralPath $QlibPython).Path
    $nautilus = (Resolve-Path -LiteralPath $NautilusPython).Path
    $dotnetExe = (Resolve-Path -LiteralPath $DotNet).Path
    $lean = (Resolve-Path -LiteralPath $LeanSource).Path
    $qlibCheckout = (Resolve-Path -LiteralPath $QlibSource).Path
    if (Test-Path -LiteralPath $OutputRoot) { throw 'Choose a new output directory; prior evidence is preserved.' }
    $run = (New-Item -ItemType Directory -Path $OutputRoot).FullName
    function Invoke-Check([string]$Name, [string]$Executable, [string[]]$Arguments) {
        Write-Host "Running $Name"
        & $Executable @Arguments *> (Join-Path $run ($Name + '.log'))
        if ($LASTEXITCODE -ne 0) { throw "$Name failed. See $run/$Name.log" }
    }
    Invoke-Check 'regressions' $qlib @('-m', 'unittest', 'discover', '-s', 'futu_bridge/tests')
    Invoke-Check 'nautilus' $nautilus @('-m', 'futu_bridge.converters.nautilus', '--capture', $capture, '--output', (Join-Path $run 'nautilus'))
    Invoke-Check 'qlib' $qlib @('-m', 'futu_bridge.converters.qlib', '--capture', $capture, '--output', (Join-Path $run 'qlib'), '--qlib-source', $qlibCheckout)
    Copy-Item -LiteralPath 'futu_bridge/converters/lean_algorithm.cs' -Destination (Join-Path $lean 'Algorithm.CSharp/LocalProviderCompatibilityAlgorithm.cs')
    Invoke-Check 'lean-build' $dotnetExe @('build', (Join-Path $lean 'Launcher/QuantConnect.Lean.Launcher.csproj'), '-c', 'Release', '--nologo', '-v', 'quiet')
    Invoke-Check 'lean-export' $qlib @('-m', 'futu_bridge.converters.lean', 'export', '--capture', $capture, '--output', (Join-Path $run 'lean'), '--lean-source', $lean)
    Invoke-Check 'lean-engine' $dotnetExe @((Join-Path $lean 'Launcher/bin/Release/QuantConnect.Lean.Launcher.dll'), '--config', (Join-Path $run 'lean/config.json'))
    Invoke-Check 'lean-verify' $qlib @('-m', 'futu_bridge.converters.lean', 'verify', '--capture', $capture, '--output', (Join-Path $run 'lean'))
    Invoke-Check 'audit' $qlib @('-m', 'futu_bridge.converters.audit', '--capture', $capture, '--run-dir', $run, '--lean-source', $lean)
    Write-Host "All checks passed. Report: $run/verification.json"
} finally {
    Pop-Location
}
