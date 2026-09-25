# Installs the Voice Keyboard desktop client (vkeyboard) on Windows.
#   & ([scriptblock]::Create((irm 'https://host/client/install.ps1'))) -Server 'https://host' -Token 'TOKEN'
# Downloads one self-contained program to %LOCALAPPDATA%\VKeyboard, removes any
# older version, pairs it, and starts it in the background (and at every
# login). No administrator rights are needed.
param(
  [Parameter(Mandatory=$true)][string]$Server,
  [Parameter(Mandatory=$true)][string]$Token
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # makes Invoke-WebRequest much faster

$arch = "amd64"
try {
  if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq "Arm64") { $arch = "arm64" }
} catch {
  if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") { $arch = "arm64" }
}

$root = Join-Path $env:LOCALAPPDATA "VKeyboard"
$exe = Join-Path $root "VKeyboard.exe"
$shim = Join-Path $root "vkeyboard.cmd"
$oldRoot = Join-Path $env:LOCALAPPDATA "VoiceKeyboard"   # releases before 1.3 and the Python client
New-Item -ItemType Directory -Force $root | Out-Null

# The program is built without a console window (so the login item is
# invisible); the .cmd shim waits for it so commands print in the terminal.
function Invoke-Program([string]$Path, [string[]]$Arguments) {
  $p = Start-Process -FilePath $Path -ArgumentList $Arguments -NoNewWindow -Wait -PassThru
  if ($p.ExitCode -ne 0) { throw "vkeyboard $($Arguments[0]) failed" }
}

Write-Host "Downloading vkeyboard for windows/$arch..."
Invoke-WebRequest "$Server/client/vkeyboard-windows-$arch.exe" -OutFile "$exe.download" -UseBasicParsing

# --- Remove older versions before installing this one.
if (Test-Path $exe) { try { Invoke-Program $exe @("stop") } catch {} }   # earlier vkeyboard
$oldExe = Join-Path $oldRoot "VoiceKeyboard.exe"
if (Test-Path $oldExe) { try { Invoke-Program $oldExe @("stop") } catch {} }  # voice-keyboard releases
$python = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%' OR Name LIKE 'py.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*voice_keyboard_client.py*" })
$python | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
if (Test-Path $oldRoot) {
  Write-Host "Removing the previous voice-keyboard client..."
  Start-Sleep -Milliseconds 500
  Remove-Item -Recurse -Force $oldRoot -ErrorAction SilentlyContinue
}
# `vkeyboard start` below removes the old login item and settings (keeping the machine id).

Move-Item -Force "$exe.download" $exe
Set-Content -Path $shim -Encoding ASCII -Value "@start `"`" /b /wait `"%~dp0VKeyboard.exe`" %*"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$parts = @(($userPath -split ";") | Where-Object { $_ -and $_ -ne $oldRoot })
if ($parts -notcontains $root) { $parts += $root }
[Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
$env:Path = "$env:Path;$root"

Invoke-Program $exe @("enroll", "--server", $Server, "--token", $Token)
Invoke-Program $exe @("start")
Write-Host "Manage it with: vkeyboard status | logs | stop | start   (open a new terminal first)"
