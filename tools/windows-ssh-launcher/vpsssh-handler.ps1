param(
  [Parameter(Mandatory = $true)]
  [string]$Url
)

$ErrorActionPreference = "Stop"

$StatusUrl = "https://nezha.xushuo.uk/status/vps-status.json"
$FallbackAllowedHosts = @(
  "4.194.211.209",
  "154.36.187.99",
  "89.144.8.91",
  "52.184.97.170",
  "35.209.42.66",
  "20.249.13.122",
  "20.78.128.47",
  "20.89.88.39",
  "20.243.208.94"
)

function Get-QueryParams {
  param([string]$Query)

  $params = @{}
  $trimmed = $Query.TrimStart("?")

  if (-not $trimmed) {
    return $params
  }

  foreach ($pair in $trimmed -split "&") {
    if (-not $pair) {
      continue
    }

    $parts = $pair -split "=", 2
    $name = [Uri]::UnescapeDataString($parts[0])
    $value = if ($parts.Count -gt 1) { [Uri]::UnescapeDataString($parts[1].Replace("+", " ")) } else { "" }
    $params[$name] = $value
  }

  return $params
}

function Assert-Match {
  param(
    [string]$Name,
    [string]$Value,
    [string]$Pattern
  )

  if (-not $Value -or $Value -notmatch $Pattern) {
    throw "Invalid ${Name}: $Value"
  }
}

function Quote-ForScript {
  param([string]$Value)

  return "'" + $Value.Replace("'", "''") + "'"
}

function Test-KeyPath {
  param([string]$Path)

  if (-not $Path) {
    return $true
  }

  if ($Path -match "[`r`n;&|<>]") {
    return $false
  }

  if ($Path -notmatch "^[A-Za-z]:\\") {
    return $false
  }

  return $true
}

function Add-AllowedHost {
  param(
    [System.Collections.Generic.List[string]]$Hosts,
    [string]$HostValue
  )

  if ($HostValue -and -not $Hosts.Contains($HostValue.ToLowerInvariant())) {
    $Hosts.Add($HostValue.ToLowerInvariant())
  }
}

function Get-HostFromSshCommand {
  param([string]$Command)

  if ($Command -match "\b[A-Za-z0-9._-]+@(?<host>[A-Za-z0-9._:-]+)") {
    return $Matches["host"]
  }

  return ""
}

function Get-AllowedHosts {
  $hosts = [System.Collections.Generic.List[string]]::new()

  foreach ($hostValue in $FallbackAllowedHosts) {
    Add-AllowedHost -Hosts $hosts -HostValue $hostValue
  }

  try {
    $status = Invoke-RestMethod -Uri $StatusUrl -TimeoutSec 4
    $servers = @()

    if ($status.servers) {
      $servers = $status.servers
    } elseif ($status.data -and $status.data.servers) {
      $servers = $status.data.servers
    }

    foreach ($server in $servers) {
      Add-AllowedHost -Hosts $hosts -HostValue $server.ip
      Add-AllowedHost -Hosts $hosts -HostValue (Get-HostFromSshCommand -Command $server.ssh)
    }
  } catch {
    # Keep the local fallback list when the status JSON is temporarily unavailable.
  }

  return $hosts
}

$uri = [Uri]$Url

if ($uri.Scheme -ne "vpsssh") {
  throw "Unsupported protocol: $($uri.Scheme)"
}

$params = Get-QueryParams -Query $uri.Query
$name = $params["name"]
$user = $params["user"]
$hostName = $params["host"]
$port = $params["port"]
$key = $params["key"]
if (-not $key) {
  $key = $params["identity"]
}

Assert-Match -Name "user" -Value $user -Pattern "^[A-Za-z0-9._-]+$"
Assert-Match -Name "host" -Value $hostName -Pattern "^[A-Za-z0-9._:-]+$"

if ($port) {
  Assert-Match -Name "port" -Value $port -Pattern "^\d{1,5}$"

  $portNumber = [int]$port
  if ($portNumber -lt 1 -or $portNumber -gt 65535) {
    throw "Invalid port: $port"
  }
}

if (-not (Test-KeyPath -Path $key)) {
  throw "Invalid key path: $key"
}

$allowedHosts = Get-AllowedHosts
if (-not $allowedHosts.Contains($hostName.ToLowerInvariant())) {
  throw "Host is not in the VPS allowlist: $hostName"
}

$target = "$user@$hostName"
$sshArgs = @()

if ($key) {
  $sshArgs += "-i"
  $sshArgs += $key
}

$sshArgs += $target

if ($port) {
  $sshArgs += "-p"
  $sshArgs += $port
}

$title = if ($name) { $name } else { $target }
$safeTitle = $title -replace "[^\p{L}\p{Nd}_. -]", "_"
$quotedArgs = $sshArgs | ForEach-Object { Quote-ForScript -Value $_ }
$sshCommand = "ssh " + ($quotedArgs -join " ")
$tempScript = Join-Path $env:TEMP ("vpsssh-" + [Guid]::NewGuid().ToString("N") + ".ps1")
$escapedTitle = $safeTitle.Replace("'", "''")
$script = @"
`$Host.UI.RawUI.WindowTitle = 'SSH - $escapedTitle'
Write-Host 'Connecting to $target ...'
Write-Host ''
$sshCommand
Write-Host ''
Write-Host 'SSH session ended. You can close this window.'
"@

Set-Content -LiteralPath $tempScript -Value $script -Encoding UTF8

Start-Process -FilePath "powershell.exe" -ArgumentList @(
  "-NoExit",
  "-ExecutionPolicy",
  "Bypass",
  "-File",
  $tempScript
)
