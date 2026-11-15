# OpenWrt for Arista access points: download the network installer and run it.
#
#   irm https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install/get.ps1 | iex
#
# It keeps everything in one folder, %USERPROFILE%\arista-openwrt: the installer,
# its own private copy of the one Python library it needs, the firmware it saves
# from each AP (backups\) and its logs (logs\).  Nothing else on this PC changes.
# Run the same line again later to get the newest installer.

$ErrorActionPreference = 'Stop'
$base = if ($env:ARISTA_OPENWRT_BASE) { $env:ARISTA_OPENWRT_BASE } else { 'https://raw.githubusercontent.com/WiFiGUY512/openwrt-arista-images/main/install' }
$dir  = if ($env:ARISTA_OPENWRT_DIR) { $env:ARISTA_OPENWRT_DIR } else { Join-Path $HOME 'arista-openwrt' }

function Find-Python {
    foreach ($c in @(@('py', '-3'), @('python'), @('python3'))) {
        try {
            $exe = $c[0]
            $rest = @($c | Select-Object -Skip 1)
            $v = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -match '^3\.(\d+)$' -and [int]$Matches[1] -ge 8) { return ,$c }
        } catch { }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host ''
    Write-Host 'This needs Python 3.8 or newer, and this PC has none.'
    Write-Host '  Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),'
    Write-Host '  or run:  winget install Python.Python.3.13'
    Write-Host 'Then open a new PowerShell window and run the same line again.'
    return
}

New-Item -ItemType Directory -Force -Path $dir, (Join-Path $dir 'backups'), (Join-Path $dir 'releases') | Out-Null
$venvPy = Join-Path $dir 'venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host 'Setting up (once, about a minute)...'
    $exe = $py[0]
    $rest = @($py | Select-Object -Skip 1)
    & $exe @rest -m venv (Join-Path $dir 'venv')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { throw 'Python could not create its private folder (venv).' }
}
& $venvPy -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('paramiko') else 1)"
if ($LASTEXITCODE -ne 0) {
    & $venvPy -m pip install --quiet --disable-pip-version-check paramiko
    if ($LASTEXITCODE -ne 0) { throw 'Could not download the SSH library (paramiko). Is this PC online?' }
}

$tool = Join-Path $dir 'arista-field.py'
try {
    Invoke-WebRequest -UseBasicParsing -Uri "$base/arista-field.py" -OutFile "$tool.new"
    if ((Get-Item "$tool.new").Length -lt 100000) { throw 'the download is too small to be the installer' }
    Move-Item -Force "$tool.new" $tool
} catch {
    Remove-Item -Force -ErrorAction SilentlyContinue "$tool.new"
    if (-not (Test-Path $tool)) { throw "Could not download the installer: $_" }
    Write-Host "Could not fetch the newest installer ($_); using the copy already here."
}

Write-Host ''
& $venvPy $tool @args
Write-Host ''
Write-Host "To run it again:  & '$venvPy' '$tool'"
