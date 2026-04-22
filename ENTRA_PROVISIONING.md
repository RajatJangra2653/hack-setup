# Entra ID Bulk User Provisioning

This module extends the OneDrive provisioner with full Entra ID (Azure AD) user lifecycle automation:
create users → issue Temporary Access Pass (TAP) → assign licenses → create groups → add memberships → assign admin role.

## Files

| File | Purpose |
|------|---------|
| `src/onedrive_provisioner/entra/` | Backend module (models, services, orchestrator) |
| `app.py` `/api/provision-users` | Flask endpoint (production) |
| `dev_server.py` | Same endpoint for local dev (port 4280) |
| `frontend/index.html` "Provision Users" tab | Web UI |
| `provision_entra_users.py` | CLI runner |
| `sample_provision_config.json` | Example config payload |
| `grant_entra_permissions.py` | One-shot helper to grant Microsoft Graph app-only permissions |

## Required Microsoft Graph permissions (app-only)

Grant **all** of these to your service principal in Entra ID → App registrations → API permissions:

| Permission | Why |
|------------|-----|
| `User.ReadWrite.All` | Create users |
| `Group.ReadWrite.All` | Create groups + manage membership |
| `UserAuthenticationMethod.ReadWrite.All` | Issue TAP credentials |
| `Organization.Read.All` | List subscribed SKUs |
| `RoleManagement.ReadWrite.Directory` | Assign Global Reader role to admins |

Grant programmatically (requires existing `AppRoleAssignment.ReadWrite.All`):
```powershell
$env:AZURE_TENANT_ID="..."; $env:AZURE_CLIENT_ID="..."; $env:AZURE_CLIENT_SECRET="..."
python grant_entra_permissions.py
```

Or grant via Azure Portal → Enterprise Applications → your app → Permissions → **Grant admin consent**.

## Tenant policy prerequisite (TAP)

Temporary Access Pass must be **enabled** in your tenant. Check at:
**Entra ID → Protection → Authentication methods → Temporary Access Pass → Enabled**.
If disabled, users will still be created but `tap` will be `null` in the response.

## Naming conventions

- **Team mode** (`mode: "team"`):  `{prefix}t{NN}-u{NN}@{domain}`
  - Sample: `nyc-esri-gcc-t01-u01@WWPS319.onmicrosoft.com`
- **Flat mode** (`mode: "flat"`): `{prefix}u{NN}@{domain}`
- **Admins**: `{prefix}admin{NN}@{domain}`
- **Team groups**: `{prefix}t{NN}-group`
- **Admin group**: `{prefix}admins`

## API

### POST /api/provision-users
```json
{
  "tenant_id": "...",
  "client_id": "...",
  "client_secret": "...",
  "config": {
    "prefix": "nyc-esri-gcc-",
    "domain": "WWPS319.onmicrosoft.com",
    "mode": "team",
    "teams": 5,
    "usersPerTeam": 10,
    "adminUsers": 2,
    "licenses": ["M365_E3", "COPILOT"],
    "tapLifetime": 240,
    "concurrency": 6,
    "skipExisting": true,
    "dryRun": false
  }
}
```
Response: `202 { "session_id": "...", "status": "running" }`

### GET /api/provision-users/{session_id}
Returns full session state including `partial_users[]` while running and `result` when complete:
```json
{
  "id": "...",
  "status": "completed",
  "processed": 52,
  "total": 52,
  "result": {
    "totalUsers": 52,
    "created": 52, "existing": 0, "failed": 0, "admins": 2,
    "groupsCreated": 6,
    "groups": ["nyc-esri-gcc-admins", "nyc-esri-gcc-t01-group", ...],
    "users": [
      {
        "userPrincipalName": "nyc-esri-gcc-t01-u01@WWPS319.onmicrosoft.com",
        "userId": "...",
        "status": "created",
        "tap": "abcd1234efgh5678",
        "tapExpires": "2025-...",
        "licenses": ["M365_E3", "COPILOT"],
        "groups": ["nyc-esri-gcc-t01-group"],
        "isAdmin": false,
        "message": null
      }
    ]
  }
}
```

## Supported license friendly names

(matched against `subscribedSkus.skuPartNumber` substring; tenant-specific suffixes are tolerated)

`M365_BUSINESS`, `M365_E3`, `M365_E5`, `COPILOT`, `TEAMS_ESSENTIALS`, `TEAMS_PREMIUM`, `POWER_BI_PRO`, `POWER_APPS`, `COPILOT_STUDIO`

You can also pass a raw skuPartNumber substring not in the catalog — it's matched directly.

## CLI

```powershell
python provision_entra_users.py sample_provision_config.json
python provision_entra_users.py sample_provision_config.json --dry-run
```

Writes `<config>.result.json` next to the input file.

## Idempotency

- `skipExisting: true` (default) — existing users (UPN match) are returned with status `existing`, no TAP re-issued, no licenses re-assigned.
- Group membership add is idempotent (already-member errors swallowed).
- Set `skipExisting: false` to fail on duplicate UPN.

## Limits

- TAP requires the tenant policy to allow it (per-user policy may also restrict).
- Licenses require user to have `usageLocation` set (the module sets `US` automatically; change in `user_service.py` if needed).
- Global Reader role activation requires `RoleManagement.ReadWrite.Directory`. The module auto-activates from template if needed.
