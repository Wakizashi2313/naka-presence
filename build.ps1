# Construit l'application (dist\NakaPresence) puis l'installateur (dist\NakaPresence-Setup.exe).
# Prérequis : Python 3.12+ et Inno Setup 6.7+ (winget install JRSoftware.InnoSetup).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python -m pip install -q -r requirements.txt
.\.venv\Scripts\python -m PyInstaller --noconfirm --onedir --windowed --name NakaPresence `
    --icon naka.ico --add-data "icon.png;." --add-data "tray.png;." naka_presence.py
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup introuvable" }
$version = (Select-String -Path naka_presence.py -Pattern '^APP_VERSION = "(.+?)"').Matches[0].Groups[1].Value
& $iscc "/DAppVersion=$version" installer.iss
