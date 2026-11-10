param([string]$PythonExe = '')
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location $repo
try {
    if (-not $PythonExe) {
        $prepared = Join-Path $repo 'tmp/python-joint/cpython-3.12.16-windows-x86_64-none/python.exe'
        $PythonExe = if (Test-Path -LiteralPath $prepared) { $prepared } else { 'python3.12' }
    }
    & $PythonExe -c 'import sys,struct; sys.exit(0 if sys.version_info[:2] == (3,12) and struct.calcsize("P") == 8 else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Use a Windows x64 Python 3.12 interpreter.' }
    if (Test-Path -LiteralPath '.venv-joint') { throw 'The shared environment already exists; verify it before changing it.' }
    & $PythonExe -m venv .venv-joint
    if ($LASTEXITCODE -ne 0) { throw 'Environment creation failed.' }
    $jointPython = (Resolve-Path '.venv-joint/Scripts/python.exe').Path
    & $jointPython scripts/build_tianshou_compat.py
    if ($LASTEXITCODE -ne 0) { throw 'Verified Tianshou compatibility wheel build failed.' }
    & $jointPython -m pip install --find-links tmp/joint-wheels -r requirements/joint-py312-windows.lock
    if ($LASTEXITCODE -ne 0) { throw 'Shared dependency installation failed.' }
    & $jointPython -m pip install --no-deps -e .
    if ($LASTEXITCODE -ne 0) { throw 'Bridge installation failed.' }
    $report = 'output/joint/runtime-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json'
    & $jointPython -m futu_bridge.joint.runtime_check --output $report
    if ($LASTEXITCODE -ne 0) { throw 'Shared runtime verification failed.' }
} finally {
    Pop-Location
}
