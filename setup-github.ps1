# Puts this folder on GitHub, as a public repository.
#
# Run it from this folder:
#       powershell -ExecutionPolicy Bypass -File .\setup-github.ps1
#
# It needs git, and the GitHub CLI if you want the repository created for you:
#       winget install Git.Git
#       winget install GitHub.cli
#
# WARNING, read this before running it.
#
#   This publishes the contents of this folder to the internet, permanently
#   enough that deleting the repository later does not unpublish what people
#   already cloned. Before the first push it prints exactly what would be
#   committed and waits for you to type YES.
#
#   .gitignore keeps out the things that must not be published: your reports,
#   your mod lists, your logs, your settings, the build folders, everything left
#   over from the old pzmodcheck name, and the Steamworks dll, which is Valve's
#   to distribute and not yours. Read the list the script prints anyway. It is
#   the last easy moment.
#
# A note on why this script is written the way it is.
#
#   $ErrorActionPreference is deliberately NOT "Stop". Windows PowerShell 5.1
#   turns anything a native program writes to stderr into an error record, and
#   under "Stop" that kills the script. git writes to stderr constantly, and
#   perfectly normally: "your current branch does not have any commits yet" is
#   an answer, not a failure. So every git call here is checked by its exit code
#   instead, which is the thing that actually means something.

$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$repoName = "pzmodmanager"
$description = "Project Zomboid mod manager: finds conflicts, fixes load order, exports the server ini."

function Invoke-Git {
    # Run git, swallow its chatter, and hand back the exit code honestly.
    $output = & git @args 2>&1
    return [pscustomobject]@{ Code = $LASTEXITCODE; Output = ($output | Out-String).Trim() }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "git is not installed. Install it with:  winget install Git.Git" -ForegroundColor Red
    exit 1
}

# ------------------------------------------------------------ the repository --

if (-not (Test-Path ".git")) {
    $r = Invoke-Git init -b main
    if ($r.Code -ne 0) { Write-Host "git init failed: $($r.Output)" -ForegroundColor Red; exit 1 }
    Write-Host "git repository created" -ForegroundColor Green
} else {
    Write-Host "git repository already here, carrying on"
}

# Clear the index and rebuild it, so an updated .gitignore actually takes effect
# on a second run. Without this, a file staged before it was ignored stays
# staged, and gets published anyway.
Invoke-Git rm -r --cached . --quiet | Out-Null
$r = Invoke-Git add -A
if ($r.Code -ne 0) { Write-Host "git add failed: $($r.Output)" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "This is what would be published:" -ForegroundColor Yellow
Write-Host ""
& git -c color.status=always status --short
Write-Host ""
$staged = @(& git diff --cached --name-only).Count
Write-Host "$staged file(s). Anything you do NOT want public, add its name to .gitignore,"
Write-Host 'then run this script again. It rebuilds the list from .gitignore every time.'
Write-Host ""

$answer = Read-Host "Type YES to publish this to GitHub, anything else to stop"
if ($answer -ne "YES") {
    Write-Host "Stopped. Nothing was pushed. The local repository is kept." -ForegroundColor Green
    exit 0
}

# --quiet returns non-zero on a repository with no commits, and says nothing.
& git rev-parse --verify --quiet HEAD | Out-Null
$hasCommits = ($LASTEXITCODE -eq 0)

if (-not $hasCommits) {
    $r = Invoke-Git commit -m "pzmodmanager: mod conflict scanner and manager for Project Zomboid"
    if ($r.Code -ne 0) { Write-Host "commit failed: $($r.Output)" -ForegroundColor Red; exit 1 }
    Write-Host "first commit made" -ForegroundColor Green
} else {
    $r = Invoke-Git commit -m "Update"
    if ($r.Code -eq 0) {
        Write-Host "commit made" -ForegroundColor Green
    } else {
        Write-Host "nothing new to commit, carrying on"
    }
}

# ---------------------------------------------------------------- to GitHub --

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "The GitHub CLI is not installed, so the last step is yours:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  1. Create an empty repository named $repoName at https://github.com/new"
    Write-Host "     Do not tick 'Add a README', the folder already has one."
    Write-Host ""
    Write-Host "  2. Then run these two lines, with your own username:"
    Write-Host ""
    Write-Host "       git remote add origin https://github.com/<your-username>/$repoName.git"
    Write-Host "       git push -u origin main"
    Write-Host ""
    Write-Host "  Or install the CLI and run this script again:  winget install GitHub.cli"
    exit 0
}

& gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Log in to GitHub first, a browser will open:" -ForegroundColor Yellow
    & gh auth login
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Login did not complete. Nothing was pushed." -ForegroundColor Red
        exit 1
    }
}

# An origin left over from an earlier attempt would make gh repo create fail.
& git remote get-url origin 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "origin already set, pushing to it"
    & git push -u origin main
    if ($LASTEXITCODE -ne 0) { Write-Host "push failed, see above" -ForegroundColor Red; exit 1 }
} else {
    & gh repo create $repoName --public --source=. --remote=origin --description $description --push
    if ($LASTEXITCODE -ne 0) { Write-Host "gh repo create failed, see above" -ForegroundColor Red; exit 1 }
}

Write-Host ""
Write-Host "Published." -ForegroundColor Green
& gh repo view --web
