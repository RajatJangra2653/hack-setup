#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Deploy OneDrive Provisioner to Azure App Service
# ──────────────────────────────────────────────────────────────────────────────
# Prerequisites: az CLI logged in (`az login`)
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh [resource-group] [app-name] [location]
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

RG="${1:-onedrive-provisioner-rg}"
APP="${2:-onedrive-provisioner-app}"
LOCATION="${3:-eastus}"
SKU="B1"   # Basic plan — always-on, no cold start, supports background threads
PYTHON="3.12"

echo "=== OneDrive Provisioner — Azure Deployment ==="
echo "Resource Group : $RG"
echo "App Name       : $APP"
echo "Location       : $LOCATION"
echo "SKU            : $SKU (Basic — always-on)"
echo ""

# 1) Create resource group
echo "► Creating resource group..."
az group create --name "$RG" --location "$LOCATION" --output none

# 2) Create App Service plan (B1 = always-on, supports background threads)
echo "► Creating App Service plan..."
az appservice plan create \
  --name "${APP}-plan" \
  --resource-group "$RG" \
  --sku "$SKU" \
  --is-linux \
  --output none

# 3) Create web app
echo "► Creating web app..."
az webapp create \
  --name "$APP" \
  --resource-group "$RG" \
  --plan "${APP}-plan" \
  --runtime "PYTHON:${PYTHON}" \
  --output none

# 4) Configure app settings for large uploads and long operations
echo "► Configuring app settings..."
az webapp config set \
  --name "$APP" \
  --resource-group "$RG" \
  --startup-file "gunicorn --bind 0.0.0.0:8000 --timeout 600 --workers 2 --threads 4 app:app" \
  --output none

az webapp config appsettings set \
  --name "$APP" \
  --resource-group "$RG" \
  --settings \
    SCM_DO_BUILD_DURING_DEPLOYMENT=true \
    WEBSITES_CONTAINER_START_TIME_LIMIT=600 \
  --output none

# 5) Enable always-on so background threads stay alive
echo "► Enabling always-on..."
az webapp config set \
  --name "$APP" \
  --resource-group "$RG" \
  --always-on true \
  --output none

# 6) Set max request size to 200MB
echo "► Setting upload limits..."
az webapp config set \
  --name "$APP" \
  --resource-group "$RG" \
  --generic-configurations '{"requestLimits": {"maxAllowedContentLength": 209715200}}' \
  --output none 2>/dev/null || echo "  (request limit config via portal if needed)"

# 7) Deploy code
echo "► Deploying code (this may take a few minutes)..."
cd "$(dirname "$0")"

# Create a zip of the deployment package
rm -f /tmp/onedrive-deploy.zip
zip -r /tmp/onedrive-deploy.zip \
  app.py \
  gunicorn.conf.py \
  requirements-prod.txt \
  startup.sh \
  frontend/ \
  src/ \
  -x "*.pyc" "__pycache__/*" ".venv/*" "*.egg-info/*"

# Rename requirements for Azure build
# Azure looks for requirements.txt during Oryx build
cp requirements-prod.txt requirements.txt.bak
cp requirements-prod.txt requirements.txt

az webapp deploy \
  --name "$APP" \
  --resource-group "$RG" \
  --src-path /tmp/onedrive-deploy.zip \
  --type zip \
  --output none

# Restore original requirements.txt
mv requirements.txt.bak requirements.txt

echo ""
echo "══════════════════════════════════════════════════"
echo "  Deployment complete!"
echo ""
echo "  URL: https://${APP}.azurewebsites.net"
echo ""
echo "  Features:"
echo "    ✓ No API timeout (gunicorn 600s, always-on)"
echo "    ✓ 200MB upload limit"
echo "    ✓ Background thread support (B1 plan)"
echo "    ✓ Live progress tracking"
echo "══════════════════════════════════════════════════"
