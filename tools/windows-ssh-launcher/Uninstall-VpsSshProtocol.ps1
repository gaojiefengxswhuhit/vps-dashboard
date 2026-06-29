$ErrorActionPreference = "Stop"

$protocolKey = "HKCU:\Software\Classes\vpsssh"

if (Test-Path $protocolKey) {
  Remove-Item -Path $protocolKey -Recurse -Force
  Write-Host "vpsssh:// protocol removed for current Windows user."
} else {
  Write-Host "vpsssh:// protocol is not installed."
}
