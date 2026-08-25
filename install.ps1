<#
.SYNOPSIS
    Put porter on PATH and add it to the Windows Terminal dropdown.

.DESCRIPTION
    Needs uv, and nothing else:

        winget install --id=astral-sh.uv -e

    Then, from the folder holding porter.py:

        .\install.ps1

    The install is editable -- the command points at this source folder, so
    edits to porter.py take effect on the next run.  Keep the folder where it
    is; moving or deleting it breaks the command.

.PARAMETER AllUsers
    Register the Windows Terminal profile for every user on the machine
    (%ProgramFiles%) instead of just you.  Needs an elevated prompt.

.PARAMETER SkipTerminalProfile
    Install the command only; leave the Windows Terminal dropdown alone.

.PARAMETER Uninstall
    Remove the command and the Windows Terminal profile.
#>
[CmdletBinding()]
param(
    [switch]$AllUsers,
    [switch]$SkipTerminalProfile,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }

$Package  = 'porter-serial'                     # what uv knows it as
$Template = Join-Path $Root 'porter.fragment.json'

# Windows Terminal reads profile fragments from Fragments\<AppName>\*.json.
# This is NOT the folder holding settings.json -- that one lives under
# %LOCALAPPDATA%\Packages\ and has nothing to do with fragments.
$ProgFiles        = if ($env:ProgramW6432) { $env:ProgramW6432 } else { $env:ProgramFiles }
$UserFragments    = Join-Path $env:LOCALAPPDATA 'Microsoft\Windows Terminal\Fragments\porter'
$MachineFragments = Join-Path $ProgFiles 'Microsoft\Windows Terminal\Fragments\porter'

function Step($msg) { Write-Host "`n$msg" -ForegroundColor Cyan }
function Say($msg)  { Write-Host "  $msg" }
function Warn($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "`nporter: $msg`n" -ForegroundColor Red; exit 1 }

function Assert-Uv {
    if (Get-Command uv -ErrorAction SilentlyContinue) { return }
    Die @'
uv is not on PATH.  Install it, open a NEW terminal, then run this again:

    winget install --id=astral-sh.uv -e
'@
}

function Invoke-Uv {
    # uv reports progress on stderr, which PowerShell would turn into a
    # terminating error while $ErrorActionPreference is 'Stop'.
    $ErrorActionPreference = 'Continue'
    & uv @args 2>&1 | ForEach-Object { Say $_ }
    if ($LASTEXITCODE -ne 0) { Die "uv $($args -join ' ') failed (exit $LASTEXITCODE)" }
}


# --------------------------------------------------------------------------
# Uninstall
# --------------------------------------------------------------------------

if ($Uninstall) {
    Step 'Removing the Windows Terminal profile'
    $removed = $false
    foreach ($dir in @($UserFragments, $MachineFragments)) {
        if (Test-Path $dir) {
            try   { Remove-Item -Recurse -Force $dir; Say "removed $dir"; $removed = $true }
            catch { Warn "could not remove $dir -- try an elevated prompt" }
        }
    }
    if (-not $removed) { Say 'nothing registered' }

    Step "Uninstalling $Package"
    Assert-Uv
    Invoke-Uv 'tool' 'uninstall' $Package

    Write-Host "`nDone.  Restart Windows Terminal to drop the profile.`n" -ForegroundColor Green
    exit 0
}


# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------

if (-not (Test-Path (Join-Path $Root 'pyproject.toml'))) {
    Die "no pyproject.toml in $Root -- run this from porter's source folder"
}
Assert-Uv

Step "Installing porter from $Root"
Invoke-Uv 'tool' 'install' '--editable' $Root '--force'

Step 'Putting it on PATH'
Invoke-Uv 'tool' 'update-shell'
# PATH is read at process start, so refresh this session's copy to verify below.
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')

$bin = (& uv tool dir --bin 2>$null | Select-Object -First 1).Trim()
$exe = Join-Path $bin 'porter.exe'
if (-not (Test-Path $exe)) { Die "uv did not leave a porter.exe in $bin" }
Say $exe

# The classic way this breaks: the source folder is on PATH and .PY is in
# PATHEXT, so a bare `porter` matches porter.py and opens in an editor.
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath) {
    $entries = @($userPath -split ';' | Where-Object { $_ } |
                 ForEach-Object { $_.TrimEnd('\') })
    if ($entries -contains $Root.TrimEnd('\')) {
        Warn "$Root is on your user PATH."
        Warn 'Take it off, or a bare porter will open porter.py in an editor.'
    }
}

if (-not $SkipTerminalProfile) {
    Step 'Registering the Windows Terminal profile'
    if (-not (Test-Path $Template)) { Die "missing $Template" }

    $fragment = Get-Content -Raw -Encoding UTF8 $Template | ConvertFrom-Json
    $fragment.PSObject.Properties.Remove('$help')
    # Point at the shim by full path: Windows Terminal may have been started
    # before PATH picked up the bin directory.
    $prof = @($fragment.profiles)[0]
    $prof.commandline = '"{0}"' -f $exe

    $dir = if ($AllUsers) { $MachineFragments } else { $UserFragments }
    try {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $file = Join-Path $dir 'porter.json'
        $utf8 = New-Object System.Text.UTF8Encoding($false)   # no BOM
        [System.IO.File]::WriteAllText($file, ($fragment | ConvertTo-Json -Depth 10), $utf8)
        Say $file
    } catch {
        if ($AllUsers) { Die "could not write to $dir -- run this from an elevated prompt" }
        throw
    }
}

Step 'Checking it runs'
# porter reports config problems on stderr, which PowerShell 7 would otherwise
# treat as a failure of the whole command.
$ErrorActionPreference = 'Continue'
& $exe --list
$rc = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($rc -ne 0) { Die "porter --list exited $rc" }

Write-Host @'

Installed.

  Restart your terminal, then porter works from any prompt.
  Restart Windows Terminal to see "porter (serial)" in the new-tab dropdown.

  Edits to porter.py are picked up on the next run -- no reinstall.
  Uninstall with:  .\install.ps1 -Uninstall

'@ -ForegroundColor Green
