# skill-gardener cron backstop: fire due cron jobs even when the desktop app is closed.
# Runs `hermes cron tick` daily at 09:50 (5 min before watchdog 09:55 / weekly 10:00).
# tick takes the cron/.tick.lock file lock, so it never double-fires alongside the
# desktop backend's own ticker.

# Locate HERMES_HOME with a multi-level fallback. Scheduled tasks often run as
# SYSTEM, whose LOCALAPPDATA points at systemprofile (which may contain a stray
# empty "hermes" dir), while the real install lives under a real user's profile.
# A genuine HERMES_HOME always contains config.yaml, so we key on that.
function Find-HermesHome {
    $candidates = @()
    if ($env:HERMES_HOME) { $candidates += $env:HERMES_HOME }
    $candidates += (Join-Path $env:LOCALAPPDATA "hermes")
    foreach ($d in (Get-ChildItem "C:\Users" -Directory -ErrorAction SilentlyContinue)) {
        $candidates += (Join-Path $d.FullName "AppData\Local\hermes")
    }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path (Join-Path $c "config.yaml"))) { return $c }
    }
    return $null
}

$hermesHome = Find-HermesHome
if (-not $hermesHome) { exit 0 }  # nothing found: exit silently, watchdog is the backstop

# Set HERMES_HOME explicitly so tick reads/writes the right cron/jobs.json
# (default probing goes astray when running as SYSTEM).
$env:HERMES_HOME = $hermesHome

$py = Join-Path $hermesHome "hermes-agent\venv\Scripts\python.exe"
if (Test-Path $py) {
    $cwd = Join-Path $hermesHome "hermes-agent"
    Push-Location $cwd
    & $py -m hermes_cli.main cron tick 2>&1 | Out-Null
    $code = $LASTEXITCODE
    Pop-Location
    exit $code
}

if (Get-Command hermes -ErrorAction SilentlyContinue) {
    & hermes cron tick 2>&1 | Out-Null
    exit $LASTEXITCODE
}

exit 0
