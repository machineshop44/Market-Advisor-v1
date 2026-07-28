# Sync local Market Advisor changes to GitHub (safe paths only).
# Skips secrets; no-ops when the working tree is clean.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$env:GIT_AUTHOR_NAME = "machineshop44"
$env:GIT_AUTHOR_EMAIL = "machineshop44@users.noreply.github.com"
$env:GIT_COMMITTER_NAME = $env:GIT_AUTHOR_NAME
$env:GIT_COMMITTER_EMAIL = $env:GIT_AUTHOR_EMAIL
$env:GIT_TERMINAL_PROMPT = "0"

git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Not a git repository: $PSScriptRoot"
    exit 1
}

# Stage known project files; never touch secrets (.gitignore already excludes settings.json etc.)
git add -- README.md .gitignore Src/*.py Src/*.bat Src/settings.example.json 2>$null
git add -u -- Src README.md .gitignore 2>$null

$status = git status --porcelain
if (-not $status) {
    Write-Host "Nothing to sync."
    exit 0
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
$msg = @"
Periodic sync: local Market Advisor updates ($stamp).

Keeps the private GitHub mirror current with the desktop working copy.
"@

git commit -m $msg
if ($LASTEXITCODE -ne 0) {
    Write-Host "Commit skipped or failed."
    exit 0
}

git -c credential.helper= -c "credential.helper=!gh auth git-credential" push origin HEAD
if ($LASTEXITCODE -ne 0) {
    Write-Error "Push failed. Check 'gh auth status'."
    exit 1
}

Write-Host "Synced to GitHub: $(git log -1 --oneline)"
exit 0
