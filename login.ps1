# Plaud login for the Docker setup, without installing Node.js on the PC.
# Runs Plaud's official login CLI in a throwaway container and saves the token in
# %USERPROFILE%\.plaud\tokens.json, which the sync container uses.
#
#   powershell -ExecutionPolicy Bypass -File .\login.ps1
#
# A link is printed: open it in a browser ON THIS PC within 2 minutes and approve.
Set-Location -LiteralPath $PSScriptRoot
$PlaudCliVersion = if ($env:PLAUD_CLI_VERSION) { $env:PLAUD_CLI_VERSION } else { "0.3.15" }
$TokenDir = if ($env:PLAUD_TOKEN_DIR) { $env:PLAUD_TOKEN_DIR } else { Join-Path $env:USERPROFILE ".plaud" }
New-Item -ItemType Directory -Force -Path $TokenDir | Out-Null

Write-Host "Plaud login: open the link printed below in a browser on this PC, then approve."
docker run --rm -it --name plaud-login `
    -p 127.0.0.1:8199:8199 `
    -e PLAUD_TELEMETRY_DISABLED=1 `
    -e "PLAUD_CLI_VERSION=$PlaudCliVersion" `
    -v "${TokenDir}:/root/.plaud" `
    -v "${PSScriptRoot}\login-forward.js:/login-forward.js:ro" `
    node:22-alpine node /login-forward.js
if ($LASTEXITCODE -ne 0) { Write-Host "Login did not finish (exit code $LASTEXITCODE)."; exit 1 }

if (docker ps -q --filter "name=^plaud-sync$") {
    Write-Host "Checking the login from the sync container ..."
    docker exec plaud-sync python sync.py --check-auth
}
