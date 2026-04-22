# ──────────────────────────────────────────────────────────────────────────────
# Provision OneDrive for the 20 failed users using SPO PowerShell (delegated).
# This is the only path that works since ACS/appregnew is retired and SharePoint
# REST APIs reject Azure AD app-only tokens for user profile operations.
#
# You must run this interactively as a tenant admin of WWPS319.
# ──────────────────────────────────────────────────────────────────────────────

$AdminUrl = "https://WWPS319-admin.sharepoint.com"

# Install module if missing
if (-not (Get-Module -ListAvailable -Name Microsoft.Online.SharePoint.PowerShell)) {
    Write-Host "Installing Microsoft.Online.SharePoint.PowerShell..." -ForegroundColor Yellow
    Install-Module Microsoft.Online.SharePoint.PowerShell -Scope CurrentUser -Force -AllowClobber
}

Import-Module Microsoft.Online.SharePoint.PowerShell -DisableNameChecking

Write-Host ">> Connecting to $AdminUrl (browser login will open)..." -ForegroundColor Cyan
Connect-SPOService -Url $AdminUrl

# Build the 20 failing user UPNs
$users = @()
1..10 | ForEach-Object { $users += "nyc-esri-gcc-t04-u$($_.ToString('00'))@WWPS319.onmicrosoft.com" }
1..10 | ForEach-Object { $users += "nyc-esri-gcc-t05-u$($_.ToString('00'))@WWPS319.onmicrosoft.com" }

Write-Host ""
Write-Host ">> Requesting personal sites for $($users.Count) users..." -ForegroundColor Cyan
$users | ForEach-Object { "  $_" }

Request-SPOPersonalSite -UserEmails $users -NoWait

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Green
Write-Host "  Queued. Personal sites usually ready in 5-10 min." -ForegroundColor Green
Write-Host ""
Write-Host "  Once provisioned, re-run the upload from the UI:" -ForegroundColor White
Write-Host "    https://onedrive-provisioner-app.azurewebsites.net/" -ForegroundColor Cyan
Write-Host "  and paste the 20 UPNs into the Users field." -ForegroundColor White
Write-Host "=====================================================" -ForegroundColor Green
