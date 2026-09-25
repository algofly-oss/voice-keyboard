# Installs the Voice Keyboard desktop client on Windows.
#   & ([scriptblock]::Create((irm 'https://host/client/install.ps1'))) -Server 'https://host' -Token 'TOKEN'
# Downloads one self-contained program to %LOCALAPPDATA%\VoiceKeyboard, pairs
# it, and starts it in the background (and at every login). Nothing else is
# installed and no administrator rights are needed.
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

$root = Join-Path $env:LOCALAPPDATA "VoiceKeyboard"
$exe = Join-Path $root "VoiceKeyboard.exe"
$shim = Join-Path $root "voice-keyboard.cmd"
New-Item -ItemType Directory -Force $root | Out-Null

# The program is built without a console window (so the login item is
# invisible); the .cmd shim waits for it so commands print in the terminal.
function Invoke-VK([string[]]$Arguments) {
  $p = Start-Process -FilePath $exe -ArgumentList $Arguments -NoNewWindow -Wait -PassThru
  if ($p.ExitCode -ne 0) { throw "voice-keyboard $($Arguments[0]) failed" }
}

if (Test-Path $exe) { try { Invoke-VK @("stop") } catch {} }   # upgrading
Write-Host "Downloading voice-keyboard for windows/$arch..."
Invoke-WebRequest "$Server/client/voice-keyboard-windows-$arch.exe" -OutFile "$exe.download" -UseBasicParsing
Move-Item -Force "$exe.download" $exe
Set-Content -Path $shim -Encoding ASCII -Value "@start `"`" /b /wait `"%~dp0VoiceKeyboard.exe`" %*"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ";") -notcontains $root) {
  [Environment]::SetEnvironmentVariable("Path", (($userPath.TrimEnd(";"), $root) -join ";").TrimStart(";"), "User")
  $env:Path = "$env:Path;$root"
}

Invoke-VK @("enroll", "--server", $Server, "--token", $Token)
Invoke-VK @("start")
Write-Host "Manage it with: voice-keyboard status | stop | start   (open a new terminal first)"
