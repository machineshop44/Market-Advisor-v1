# Run once on the Plex PC after unzip (same folder as MarketAdvisor.exe).
# Restores Robinhood session pickle + E*TRADE Windows Credential Manager secrets.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$tokensDir = Join-Path $env:USERPROFILE ".tokens"
$pickleSrc = Join-Path $here "Restore-Tokens\robinhood.pickle"
$etJson = Join-Path $here "Restore-Tokens\etrade_keyring.json"

if (Test-Path $pickleSrc) {
  New-Item -ItemType Directory -Path $tokensDir -Force | Out-Null
  Copy-Item $pickleSrc (Join-Path $tokensDir "robinhood.pickle") -Force
  Write-Host "Restored Robinhood pickle -> $tokensDir\robinhood.pickle"
} else {
  Write-Warning "No robinhood.pickle in Restore-Tokens - RH may need Settings → Connect."
}

if (Test-Path $etJson) {
  $env:MA_ETRADE_KEYRING_JSON = $etJson
  py -3.12 -c @"
import json, os, sys
try:
    import keyring
except Exception as e:
    print('keyring not installed:', e)
    print('Run: py -3.12 -m pip install keyring')
    sys.exit(1)
path = os.environ.get('MA_ETRADE_KEYRING_JSON') or ''
data = json.load(open(path, encoding='utf-8'))
svc = data.get('service') or 'MarketAdvisor.ETrade'
n = 0
for name, value in (data.get('secrets') or {}).items():
    if value:
        keyring.set_password(svc, name, value)
        n += 1
print('Restored %d E*TRADE secret(s) into Windows Credential Manager (%s)' % (n, svc))
"@
  $code = $LASTEXITCODE
  Remove-Item Env:MA_ETRADE_KEYRING_JSON -ErrorAction SilentlyContinue
  if ($code -ne 0) {
    Write-Warning "E*TRADE keyring restore failed - you may need: py -3.12 -m pip install keyring"
  }
} else {
  Write-Warning "No etrade_keyring.json - E*TRADE may need reauth."
}

Write-Host ""
Write-Host "Done. Launch MarketAdvisor.exe next."
Write-Host "If RH/ET still ask to sign in, tokens were expired or machine-bound - use Settings → Connect."
