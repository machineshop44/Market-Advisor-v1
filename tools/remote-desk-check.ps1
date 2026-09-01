# Remote Market Advisor desk check — no Python required (Windows PowerShell).
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\tools\remote-desk-check.ps1 `
#     -Url "https://192.168.1.50:8791" -Token "YOUR_READ_TOKEN"
#
# Or with monitor user/pass:
#   powershell -ExecutionPolicy Bypass -File .\tools\remote-desk-check.ps1 `
#     -Url "https://192.168.1.50:8791" -User "monitor" -Password "secret"

param(
    [string]$Url = $env:MARKET_ADVISOR_URL,
    [string]$Token = $env:MARKET_ADVISOR_TOKEN,
    [string]$User = $env:MARKET_ADVISOR_USER,
    [string]$Password = $env:MARKET_ADVISOR_PASS,
    [switch]$Full,
    [switch]$Json
)

if (-not $Url) { $Url = "https://127.0.0.1:8791" }
$Url = $Url.TrimEnd("/")

# Self-signed Plex monitor cert — skip TLS verify (same as companion)
if ($PSVersionTable.PSVersion.Major -ge 6) {
    $prev = $null
    if (Get-Command Get-Variable -ErrorAction SilentlyContinue) {
        $prev = [System.Net.ServicePointManager]::ServerCertificateValidationCallback
    }
}
try {
    add-type @"
using System.Net; using System.Security.Cryptography.X509Certificates;
public class TrustAllCerts { public static bool Handler(object s,X509Certificate c,X509Chain ch,System.Net.Security.SslPolicyErrors e){return true;} }
"@
    [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
} catch { }

function Invoke-DeskApi($Path) {
    $uri = "$Url$Path"
    $headers = @{ Accept = "application/json" }
    if ($Token) {
        $headers["Authorization"] = "Bearer $Token"
    } elseif ($User) {
        $pair = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${User}:${Password}"))
        $headers["Authorization"] = "Basic $pair"
    }
    try {
        $resp = Invoke-WebRequest -Uri $uri -Headers $headers -UseBasicParsing -TimeoutSec 15
        return $resp.Content | ConvertFrom-Json
    } catch {
        Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
        if ($_.Exception.Response) {
            Write-Host "HTTP $([int]$_.Exception.Response.StatusCode)" -ForegroundColor Red
        }
        exit 2
    }
}

if ($Full) {
    $digest = Invoke-DeskApi "/api/agent/digest"
    $snags = Invoke-DeskApi "/api/agent/snags"
    if ($Json) {
        @{ digest = $digest; snags = $snags } | ConvertTo-Json -Depth 8
        exit 0
    }
    if ($digest.summary_text) { Write-Host $digest.summary_text }
    Write-Host ""
    $snags | ConvertTo-Json -Depth 6
} else {
    $snags = Invoke-DeskApi "/api/agent/snags"
    if ($Json) {
        $snags | ConvertTo-Json -Depth 8
    } else {
        Write-Host "Watchdog: $($snags.status.ToUpper()) — $($snags.summary)" -ForegroundColor $(if ($snags.status -eq 'critical') {'Red'} elseif ($snags.status -eq 'warn') {'Yellow'} else {'Green'})
        foreach ($s in $snags.snags) {
            $b = if ($s.broker) { " [$($s.broker)]" } else { "" }
            Write-Host "  • $($s.severity.ToUpper())$b`: $($s.message)"
            if ($s.hint) { Write-Host "    -> $($s.hint)" -ForegroundColor DarkGray }
        }
    }
}

$code = switch ($snags.status) { 'critical' { 1 } 'warn' { 1 } default { 0 } }
exit $code
