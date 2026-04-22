# ──────────────────────────────────────────────────────────────────────────────
# Deploy OneDrive Provisioner to Azure App Service (PowerShell)
# ──────────────────────────────────────────────────────────────────────────────
# Prerequisites: az CLI logged in (`az login`)
#
# Usage:
#   .\deploy.ps1 [-RG "mygroup"] [-AppName "myapp"] [-Location "eastus"]
# ──────────────────────────────────────────────────────────────────────────────

param(
    [string]$RG = "onedrive-provisioner-rg",
    [string]$AppName = "onedrive-provisioner-app",
    [string]$Location = "eastus"
)

$ErrorActionPreference = "Stop"
$SKU = "B1"
$Python = "3.12"

Write-Host "=== OneDrive Provisioner - Azure Deployment ===" -ForegroundColor Cyan
Write-Host "Resource Group : $RG"
Write-Host "App Name       : $AppName"
Write-Host "Location       : $Location"
Write-Host "SKU            : $SKU (Basic - always-on, no timeout)"
Write-Host ""

# 1) Create resource group
Write-Host ">> Creating resource group..." -ForegroundColor Yellow
az group create --name $RG --location $Location --output none

# 2) Create App Service plan (B1 = always-on, supports background threads)
Write-Host ">> Creating App Service plan..." -ForegroundColor Yellow
az appservice plan create `
    --name "$AppName-plan" `
    --resource-group $RG `
    --sku $SKU `
    --is-linux `
    --output none

# 3) Create web app
Write-Host ">> Creating web app..." -ForegroundColor Yellow
az webapp create `
    --name $AppName `
    --resource-group $RG `
    --plan "$AppName-plan" `
    --runtime "PYTHON:$Python" `
    --output none

# 4) Configure startup command (gunicorn with 600s timeout for long uploads)
Write-Host ">> Configuring startup and settings..." -ForegroundColor Yellow
az webapp config set `
    --name $AppName `
    --resource-group $RG `
    --startup-file "gunicorn --bind 0.0.0.0:8000 --timeout 600 --workers 1 --threads 8 --chdir /home/site/wwwroot app:app" `
    --output none

az webapp config appsettings set `
    --name $AppName `
    --resource-group $RG `
    --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true WEBSITES_CONTAINER_START_TIME_LIMIT=600 `
    --output none

# 5) Enable always-on (keeps app running, background threads stay alive)
Write-Host ">> Enabling always-on..." -ForegroundColor Yellow
az webapp config set `
    --name $AppName `
    --resource-group $RG `
    --always-on true `
    --output none

# 6) Create deployment zip
Write-Host ">> Creating deployment package..." -ForegroundColor Yellow
$zipPath = Join-Path $env:TEMP "onedrive-deploy.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath }

# Copy requirements-prod.txt as requirements.txt for Azure Oryx build
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $scriptDir

Copy-Item "requirements-prod.txt" "requirements.txt" -Force

# Create zip with required files
$filesToZip = @(
    "app.py",
    "gunicorn.conf.py",
    "requirements.txt",
    "startup.sh"
)
$foldersToZip = @(
    "frontend",
    "src"
)

# Use a staging directory for clean zip
$staging = Join-Path $env:TEMP "onedrive-staging"
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging | Out-Null

foreach ($f in $filesToZip) {
    if (Test-Path $f) { Copy-Item $f $staging }
}
foreach ($d in $foldersToZip) {
    if (Test-Path $d) {
        Copy-Item $d (Join-Path $staging $d) -Recurse -Exclude "*.pyc","__pycache__",".venv","*.egg-info"
    }
}

Compress-Archive -Path "$staging\*" -DestinationPath $zipPath -Force
Remove-Item $staging -Recurse -Force

# Restore original requirements.txt
Copy-Item "requirements-prod.txt" "requirements.txt" -Force
Pop-Location

Write-Host "   Package: $zipPath ($('{0:N1}' -f ((Get-Item $zipPath).Length / 1MB)) MB)"

# 7) Deploy
Write-Host ">> Deploying to Azure (this may take a few minutes)..." -ForegroundColor Yellow
az webapp deploy `
    --name $AppName `
    --resource-group $RG `
    --src-path $zipPath `
    --type zip `
    --output none

$url = "https://$AppName.azurewebsites.net"

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Green
Write-Host "  Deployment complete!" -ForegroundColor Green
Write-Host ""
Write-Host "  URL: $url" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Features:" -ForegroundColor White
Write-Host "    - No API timeout (gunicorn 600s, always-on)"
Write-Host "    - 200MB upload limit"
Write-Host "    - Background thread support (B1 plan)"
Write-Host "    - Live progress tracking"
Write-Host "=====================================================" -ForegroundColor Green
Write-Host ""

# Open in browser
Start-Process $url
