# Windows version of run.sh: make sure the conda env exists, log in to Plaud if needed,
# download every recording, transcribe, summarise, tag, and open the local viewer.
#
#   .\run.ps1                 # everything
#   .\run.ps1 --limit 5       # try the 5 newest first
#   .\run.ps1 --no-local      # only fetch audio + Plaud's transcripts (light)
#   .\run.ps1 -Unattended     # for the scheduled task: no browser, output to library\auto.log
#
# If scripts are blocked:  powershell -ExecutionPolicy Bypass -File .\run.ps1
param(
    [switch]$Unattended,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$SyncArgs
)
Set-Location -LiteralPath $PSScriptRoot

$EnvName = "plaud_transcripts"
$EnvFile = "environment.portable.yml"
# Plaud's official CLI is used only for the browser login; pinned to a reviewed version.
$PlaudCliVersion = if ($env:PLAUD_CLI_VERSION) { $env:PLAUD_CLI_VERSION } else { "0.3.15" }
$Library = if ($env:PLAUD_LIBRARY) { $env:PLAUD_LIBRARY } else { Join-Path $PSScriptRoot "library" }
$Log = Join-Path $Library "auto.log"
if ($Unattended) { $env:NO_OPEN = "1"; $env:NO_LOGIN = "1" }
if (-not $SyncArgs) { $SyncArgs = @() }
New-Item -ItemType Directory -Force -Path $Library | Out-Null

function Say([string]$Text) {
    if ($Unattended) { Add-Content -LiteralPath $Log -Value $Text -Encoding UTF8 } else { Write-Host $Text }
}
function Die([string]$Text) { Say "ERROR: $Text"; exit 1 }

# --- conda -------------------------------------------------------------------
$Conda = $env:CONDA_EXE
if (-not $Conda -or -not (Test-Path -LiteralPath $Conda)) {
    $found = Get-Command conda.exe, conda.bat -ErrorAction SilentlyContinue | Select-Object -First 1
    $Conda = if ($found) { $found.Source } else { $null }
}
if (-not $Conda) {
    $bases = @("$env:USERPROFILE\miniconda3", "$env:USERPROFILE\miniforge3", "$env:USERPROFILE\anaconda3",
               "$env:LOCALAPPDATA\miniconda3", "$env:LOCALAPPDATA\miniforge3",
               "$env:ProgramData\miniconda3", "$env:ProgramData\miniforge3", "$env:ProgramData\anaconda3")
    foreach ($base in $bases) {
        if (Test-Path -LiteralPath "$base\Scripts\conda.exe") { $Conda = "$base\Scripts\conda.exe"; break }
    }
}
if (-not $Conda) { Die "conda not found. Install Miniconda or Miniforge, or set CONDA_EXE." }

function Get-EnvPrefix {
    $list = (& $Conda env list --json | Out-String | ConvertFrom-Json).envs
    $list | Where-Object { (Split-Path $_ -Leaf) -eq $EnvName } | Select-Object -First 1
}
$Prefix = Get-EnvPrefix
if (-not $Prefix) {
    if (-not (Test-Path -LiteralPath $EnvFile)) { Die "$EnvFile is missing." }
    Say "Creating conda environment '$EnvName' (first run only) ..."
    & $Conda env create -f $EnvFile
    if ($LASTEXITCODE -ne 0) { Die "conda env create failed." }
    $Prefix = Get-EnvPrefix
    if (-not $Prefix) { Die "conda environment '$EnvName' was not created." }
}
$Stamp = Join-Path $Prefix ".plaud_env_stamp"
if (-not (Test-Path -LiteralPath $Stamp) -or (Get-Item -LiteralPath $EnvFile).LastWriteTime -gt (Get-Item -LiteralPath $Stamp).LastWriteTime) {
    if (Test-Path -LiteralPath $Stamp) {   # environment file changed
        Say "Updating conda environment '$EnvName' ..."
        & $Conda env update -n $EnvName -f $EnvFile
        if ($LASTEXITCODE -ne 0) { Die "conda env update failed." }
    }
    Set-Content -LiteralPath $Stamp -Value (Get-Date -Format o)
}
# What "conda activate" would put on PATH: the env's python, ffmpeg and DLLs.
$env:PATH = "$Prefix;$Prefix\Library\mingw-w64\bin;$Prefix\Library\usr\bin;$Prefix\Library\bin;$Prefix\Scripts;$env:PATH"
$env:PYTHONUTF8 = "1"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
$Py = Join-Path $Prefix "python.exe"
if (-not (Test-Path -LiteralPath $Py)) { Die "python.exe not found in $Prefix." }

if ($SyncArgs -contains "--help" -or $SyncArgs -contains "-h") { & $Py sync.py --help; exit $LASTEXITCODE }

# --- one run at a time ---------------------------------------------------------
$Lock = Join-Path $Library ".run.lock"
if (Test-Path -LiteralPath $Lock) {
    $other = Get-Content -LiteralPath (Join-Path $Lock "pid") -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($other -and (Get-Process -Id $other -ErrorAction SilentlyContinue)) {
        Say "Another run is in progress (pid $other). Nothing to do."
        exit 0
    }
    Remove-Item -LiteralPath $Lock -Recurse -Force   # stale lock from a run that was killed
}
New-Item -ItemType Directory -Force -Path $Lock | Out-Null
Set-Content -LiteralPath (Join-Path $Lock "pid") -Value $PID

# --- login ---------------------------------------------------------------------
function Login {
    if ($env:NO_LOGIN) { throw "Plaud login is missing or expired. Run .\run.ps1 by hand once to log in." }
    $npx = Get-Command npx.cmd -ErrorAction SilentlyContinue
    if (-not $npx) { throw "npx (Node.js) is needed once for the Plaud login." }
    Write-Host ""
    Write-Host "Plaud login: your browser will open. Approve access there, then come back."
    $env:PLAUD_TELEMETRY_DISABLED = "1"
    & $npx.Source --yes "@plaud-ai/cli@$PlaudCliVersion" login
    if ($LASTEXITCODE -ne 0) { throw "Plaud login failed." }
}

function Sync {
    if ($Unattended) {
        # Native output goes to files, then is appended to the log (keeps it UTF-8).
        $out = Join-Path $Library "auto.out.tmp"; $err = Join-Path $Library "auto.err.tmp"
        $quoted = @("sync.py") + $SyncArgs | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }
        $proc = Start-Process -FilePath $Py -ArgumentList $quoted -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError $err
        foreach ($file in $out, $err) {
            if (Test-Path -LiteralPath $file) {
                Get-Content -LiteralPath $file -Encoding UTF8 | Add-Content -LiteralPath $Log -Encoding UTF8
                Remove-Item -LiteralPath $file -Force
            }
        }
        return $proc.ExitCode
    }
    & $Py sync.py @SyncArgs | Out-Host
    return $LASTEXITCODE
}

$rc = 1
try {
    $TokenFile = if ($env:PLAUD_TOKEN_FILE) { $env:PLAUD_TOKEN_FILE } else { Join-Path $env:USERPROFILE ".plaud\tokens.json" }
    if ($Unattended) { Say ("--- " + (Get-Date -Format "yyyy-MM-dd HH:mm") + " ---") }
    if (-not (Test-Path -LiteralPath $TokenFile)) { Login }
    $rc = Sync
    if ($rc -eq 2) {   # saved login expired or was revoked: log in again and retry once
        Login
        $rc = Sync
    }
    $Index = Join-Path $Library "index.html"
    if ((Test-Path -LiteralPath $Index) -and -not $env:NO_OPEN) { Start-Process $Index }
}
catch { Say "ERROR: $($_.Exception.Message)"; $rc = 1 }
finally { Remove-Item -LiteralPath $Lock -Recurse -Force -ErrorAction SilentlyContinue }
exit $rc
