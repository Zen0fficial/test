<#
.SYNOPSIS
Install or manage the project-local MongoDB Community instance for Qlib tasks.
.DESCRIPTION
Uses the official pinned ZIP and SHA-256. Does not install a Windows service,
alter PATH, or modify Python packages. The database binds only 127.0.0.1:27027.
Data and logs remain under the repository's ignored tmp directory.
#>
param([ValidateSet('Install', 'Start', 'Stop', 'Status')][string]$Action = 'Status')

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$mongoVersion = '8.0.34'
$archiveHash = '6b021a0864494e4decf278b36477a29eb36072432773e4e62c2f42c302630db0'
$installRoot = Join-Path $repoRoot "tmp/mongodb-$mongoVersion"
$archive = Join-Path $repoRoot "tmp/mongodb-windows-x86_64-$mongoVersion.zip"
$stateRoot = Join-Path $repoRoot 'tmp/joint-mongodb'
$pidFile = Join-Path $stateRoot 'process.json'

if ($Action -eq 'Install') {
    if (-not (Test-Path -LiteralPath $archive)) {
        Invoke-WebRequest -Uri "https://fastdl.mongodb.org/windows/mongodb-windows-x86_64-$mongoVersion.zip" `
            -OutFile $archive
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $archiveHash) {
        throw 'The MongoDB ZIP does not match the official pinned SHA-256.'
    }
    if (-not (Test-Path -LiteralPath $installRoot)) {
        Expand-Archive -LiteralPath $archive -DestinationPath $installRoot
    }
}

$executables = @(Get-ChildItem -LiteralPath $installRoot -Recurse -Filter 'mongod.exe' -ErrorAction SilentlyContinue)
if ($executables.Count -ne 1) { throw 'Run -Action Install to prepare exactly one verified MongoDB binary.' }
$executable = $executables[0].FullName

if ($Action -eq 'Install') {
    & $executable --version
    if ($LASTEXITCODE -ne 0) { throw 'MongoDB does not run on this PC.' }
    Write-Output "Installed project-local MongoDB at $executable"
    exit
}

if ($Action -eq 'Start') {
    if (Get-NetTCPConnection -LocalPort 27027 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Port 27027 is occupied. Inspect -Action Status; an existing process will not be replaced.'
    }
    New-Item -ItemType Directory -Path (Join-Path $stateRoot 'data') -Force | Out-Null
    $arguments = @('--bind_ip', '127.0.0.1', '--port', '27027', '--dbpath',
        ('"' + (Join-Path $stateRoot 'data') + '"'), '--logpath',
        ('"' + (Join-Path $stateRoot 'mongod.log') + '"'), '--logappend', '--wiredTigerCacheSizeGB', '0.5')
    $process = Start-Process -FilePath $executable -ArgumentList $arguments -WindowStyle Hidden -PassThru
    @{pid = $process.Id; executable = $executable; version = $mongoVersion; port = 27027;
        archive_sha256 = $archiveHash; uri = 'mongodb://127.0.0.1:27027'} |
        ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
    Write-Output "Started MongoDB PID $($process.Id); verify readiness through Qlib's existing pymongo client."
    exit
}

if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No project MongoDB process record.'; exit }
$record = Get-Content -Raw -LiteralPath $pidFile | ConvertFrom-Json
$owned = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
if (-not $owned) { Write-Output 'The project MongoDB instance is stopped.'; exit }
if ($owned.Path -ne $executable) { throw 'PID ownership differs from the recorded MongoDB binary.' }

if ($Action -eq 'Status') {
    $record
    Get-NetTCPConnection -OwningProcess $owned.Id -State Listen -ErrorAction SilentlyContinue |
        Select-Object LocalAddress, LocalPort
    exit
}

# Request a normal database shutdown, not forceful process termination.
$python = Join-Path $repoRoot '.venv-joint/Scripts/python.exe'
@'
from pymongo import MongoClient
from pymongo.errors import AutoReconnect
client = MongoClient('mongodb://127.0.0.1:27027', serverSelectionTimeoutMS=5000)
try:
    client.admin.command('shutdown')
except AutoReconnect:
    pass
finally:
    client.close()
'@ | & $python -
if ($LASTEXITCODE -ne 0) { throw 'MongoDB shutdown request failed.' }
$owned.WaitForExit(30000) | Out-Null
if (-not $owned.HasExited) { throw 'MongoDB has not completed its shutdown; inspect the local log.' }
Write-Output 'Project MongoDB shut down cleanly; its files remain available for the next run.'
