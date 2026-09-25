param(
  [Parameter(Mandatory=$true)][string]$Server,
  [Parameter(Mandatory=$true)][string]$Token,
  [string]$Room = "voice-keyboard"
)
$ErrorActionPreference = "Stop"
$Root = Join-Path $env:LOCALAPPDATA "VoiceKeyboard"
New-Item -ItemType Directory -Force $Root | Out-Null
py -m venv (Join-Path $Root "venv")
& (Join-Path $Root "venv\Scripts\python.exe") -m pip install --upgrade pip | Out-Null
& (Join-Path $Root "venv\Scripts\pip.exe") install websockets pynput | Out-Null
Invoke-WebRequest "$Server/client/voice_keyboard_client.py" -OutFile (Join-Path $Root "voice_keyboard_client.py")
& (Join-Path $Root "venv\Scripts\python.exe") (Join-Path $Root "voice_keyboard_client.py") enroll --server $Server --token $Token --room $Room
Write-Host "Installed. Start it with: voice-keyboard start"
