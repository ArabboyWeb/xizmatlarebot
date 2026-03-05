Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host 'Virtual environment yaratilmoqda...'
    python -m venv .venv
}

Write-Host 'Dependencylar o''rnatilmoqda...'
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

if (Test-Path 'bot.lock') {
    Remove-Item 'bot.lock' -Force
}

$oldBots = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -eq 'python.exe' -and
        $_.CommandLine -and
        $_.CommandLine -like '*bot.py*' -and
        $_.CommandLine -notlike "*$PID*"
    }

foreach ($proc in $oldBots) {
    try {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
        Write-Host "To''xtatildi: PID=$($proc.ProcessId)"
    } catch {
        Write-Warning "To''xtatib bo''lmadi: PID=$($proc.ProcessId)"
    }
}

Write-Host 'Bot ishga tushirilmoqda...'
& $venvPython bot.py
