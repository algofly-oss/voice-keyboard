# Trusts this server's local certificate authority on Windows, so Edge and
# Chrome accept its HTTPS certificate (and allow the microphone).
#   powershell -c "[Net.ServicePointManager]::ServerCertificateValidationCallback={$true}; irm https://<server>/client/trust-ca.ps1 | iex"
# It adds the CA to your user's trusted roots; no administrator rights needed.
$ErrorActionPreference = "Stop"
$server = $env:VK_SERVER
if (-not $server) {
  # When piped from https://<server>/client/trust-ca.ps1 the server fills this in.
  $server = "__VK_SERVER__"
}
$cert = Join-Path $env:TEMP "vkeyboard-ca.crt"
[Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }  # only for the certificate itself
Invoke-WebRequest "$server/ca.crt" -OutFile $cert -UseBasicParsing
[Net.ServicePointManager]::ServerCertificateValidationCallback = $null
Import-Certificate -FilePath $cert -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
Remove-Item $cert
Write-Host "Done. Restart the browser, then open the https:// address."
Write-Host "Firefox: set security.enterprise_roots.enabled to true in about:config to use this CA."
