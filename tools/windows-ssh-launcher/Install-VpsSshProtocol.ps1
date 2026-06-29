$ErrorActionPreference = "Stop"

$handlerPath = Join-Path $PSScriptRoot "vpsssh-handler.ps1"
$powershellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$protocolKey = "HKCU:\Software\Classes\vpsssh"
$commandKey = Join-Path $protocolKey "shell\open\command"
$command = '"' + $powershellPath + '" -NoProfile -ExecutionPolicy Bypass -File "' + $handlerPath + '" "%1"'

if (-not (Test-Path $handlerPath)) {
  throw "Cannot find handler script: $handlerPath"
}

New-Item -Path $protocolKey -Force | Out-Null
Set-ItemProperty -Path $protocolKey -Name "(default)" -Value "URL:VPS SSH Launcher"
Set-ItemProperty -Path $protocolKey -Name "URL Protocol" -Value ""

New-Item -Path $commandKey -Force | Out-Null
Set-ItemProperty -Path $commandKey -Name "(default)" -Value $command

Write-Host "vpsssh:// protocol installed for current Windows user."
Write-Host "Handler: $handlerPath"
