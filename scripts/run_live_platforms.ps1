param(
    [double]$Seconds = 0,
    [double]$DisconnectAfter = 0,
    [string]$OutputRoot = ('output/live_platforms/run-' + (Get-Date -Format 'yyyyMMdd-HHmmss')),
    [string]$ProviderUrl = 'http://127.0.0.1:8765',
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location $repo
$children = @()
$run = $null
try {
    if ($Seconds -lt 0 -or $DisconnectAfter -lt 0) { throw 'Durations must be nonnegative.' }
    if ($Seconds -gt 0 -and $DisconnectAfter -gt 0 -and $Seconds -lt $DisconnectAfter + 20) {
        throw 'Allow at least 20 seconds of live activity after the forced disconnect.'
    }
    $qlib = (Resolve-Path '.venv-joint/Scripts/python.exe').Path
    $nautilus = $qlib
    $dotnet = (Resolve-Path 'tmp/dotnet/dotnet.exe').Path
    $lean = (Resolve-Path 'tmp/lean-src').Path
    if (Test-Path -LiteralPath $OutputRoot) { throw 'Choose a new output folder to preserve earlier evidence.' }
    $run = (New-Item -ItemType Directory -Path $OutputRoot).FullName
    if (-not $SkipBuild) {
        Copy-Item -LiteralPath 'futu_bridge/converters/lean_live.cs' -Destination (Join-Path $lean 'Algorithm.CSharp/LocalFutuLive.cs')
        & $dotnet build (Join-Path $lean 'Launcher/QuantConnect.Lean.Launcher.csproj') -c Release --nologo -v quiet *> (Join-Path $run 'lean-build.log')
        if ($LASTEXITCODE -ne 0) { throw "LEAN build failed. See $run/lean-build.log" }
    }
    & $qlib -m futu_bridge.converters.lean_live --output (Join-Path $run 'lean') --lean-source $lean --base-url $ProviderUrl --seconds $Seconds --disconnect-after $DisconnectAfter
    if ($LASTEXITCODE -ne 0) { throw 'LEAN live configuration failed.' }
    $nArgs = @('-m', 'futu_bridge.converters.nautilus_live', '--symbol', '00700', '--base-url', $ProviderUrl,
        '--output', ('"' + (Join-Path $run 'nautilus') + '"'), '--seconds', "$Seconds", '--disconnect-after', "$DisconnectAfter")
    $children += Start-Process -FilePath $nautilus -ArgumentList $nArgs -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $run 'nautilus.stdout.log') -RedirectStandardError (Join-Path $run 'nautilus.stderr.log')
    $lArgs = @(('"' + (Join-Path $lean 'Launcher/bin/Release/QuantConnect.Lean.Launcher.dll') + '"'), '--config',
        ('"' + (Join-Path $run 'lean/config.json') + '"'))
    $children += Start-Process -FilePath $dotnet -ArgumentList $lArgs -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $run 'lean.stdout.log') -RedirectStandardError (Join-Path $run 'lean.stderr.log')
    Write-Host "Live HK.00700 feeds started. Output: $run"
    Write-Host 'Create a STOP file in each platform output folder to stop gracefully.'
    while (@($children | Where-Object { -not $_.HasExited }).Count -gt 0) {
        foreach ($child in $children) {
            if ($child.HasExited -and $child.ExitCode -ne 0) { throw "Live converter exited with $($child.ExitCode). Inspect $run logs." }
        }
        Start-Sleep -Milliseconds 500
    }
    foreach ($child in $children) {
        if ($child.ExitCode -ne 0) { throw "Live converter exited with $($child.ExitCode). Inspect $run logs." }
    }
    if ($Seconds -eq 0) {
        Write-Host "Continuous feeds stopped. Reports: $run/<platform>/run.json"
        Write-Host 'Use a bounded -Seconds run for exhaustive verification; long-running journals rotate.'
        return
    }
    foreach ($platform in @('nautilus', 'lean')) {
        $verifyArgs = @('-m', 'futu_bridge.converters.live_verify', '--output', (Join-Path $run $platform))
        if ($DisconnectAfter -gt 0) { $verifyArgs += '--require-reconnect' }
        & $qlib @verifyArgs
        if ($LASTEXITCODE -ne 0) { throw "$platform live verification failed. Quiet/closed markets do not count as a live pass." }
    }
    Write-Host "Both live checks passed: $run"
} finally {
    if ($run) {
        foreach ($platform in @('nautilus', 'lean')) {
            $folder = Join-Path $run $platform
            if ((Test-Path -LiteralPath $folder) -and @($children | Where-Object { -not $_.HasExited }).Count -gt 0) {
                New-Item -ItemType File -Path (Join-Path $folder 'STOP') -Force | Out-Null
            }
        }
    }
    foreach ($child in $children) {
        if (-not $child.HasExited -and -not $child.WaitForExit(30000)) { Stop-Process -Id $child.Id }
    }
    Pop-Location
}
