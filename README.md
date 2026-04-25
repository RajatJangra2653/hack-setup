# OneDrive Provisioner

Production-grade automation that **provisions OneDrive for Business** for users in an Azure AD tenant and **uploads a folder/file tree** into each user's drive — exposed both as a **CLI** and as an **MCP (Model Context Protocol) server**.

Built on top of **Microsoft Graph v1.0** with app-only (service principal) auth via **MSAL** (client secret OR certificate), full retry/throttling support, parallel bulk execution, idempotent uploads, dry-run, and structured JSON reporting.

---

## Features

- **App-only auth** with MSAL — client secret *or* certificate (preferred).
- **Idempotent uploads** — files with matching size are skipped.
- **Simple + chunked uploads** — large files use Graph upload sessions with 320 KiB-aligned chunks.
- **Provisioning waiter** — exponential backoff while OneDrive personal site is being created.
- **Bulk + parallel** — configurable concurrency, per-user error isolation.
- **Throttling-aware** — honours `Retry-After`, exponential backoff with jitter on 429/5xx/network errors.
- **Pluggable file sources** — local folder *or* Azure Blob (`https://<acct>.blob.core.windows.net/<container>/<prefix>`).
- **Dry-run** mode and CSV/JSON reporting.
- **Structured JSON logs** via `structlog`.
- **Two interfaces** — `onedrive-provisioner` (CLI) and `onedrive-provisioner-mcp` (stdio MCP server).

---

## Architecture

```
CLI / MCP server  →  Orchestrator  →  GraphClient  →  Microsoft Graph
                          │              ▲
                          ├── Auth (MSAL: secret | certificate)
                          ├── UserResolver  (resolve UPN/objectId, list members)
                          ├── OneDriveProvisioner (ensure /users/{id}/drive)
                          └── Uploader (simple PUT or chunked upload session)
                                  └── FileSource (local folder | Azure Blob)
```

See [src/onedrive_provisioner/](src/onedrive_provisioner/) for the modular layout.

---

## Setup

### 1. Create an Azure AD App Registration

1. Azure Portal → **Microsoft Entra ID → App registrations → New registration**.
2. Add **API permissions** (Microsoft Graph → **Application permissions**):
   - `User.Read.All`
   - `Files.ReadWrite.All`
   - `Sites.ReadWrite.All`
3. Click **Grant admin consent** for your tenant.
4. Create a credential — **either**:
   - **Client secret** (Certificates & secrets → New client secret), **or**
   - **Certificate** (upload a .cer; keep the matching PEM private key locally) — *preferred for production*.

> ⚠️ Least privilege: these permissions allow tenant-wide access. Restrict who can use the service principal and store credentials in Key Vault / GitHub secrets.

### Security hardening notes

- Scheduled jobs do **not** persist `client_secret` values in Blob Storage. Job records store only `tenant_id`, `client_id`, and a `client_secret_ref`.
- No new Azure resources are required for this behavior. By default, when a schedule is created from the UI, the app stores the submitted secret only in the running process and persists an ephemeral `connection_id` reference. This protects Blob Storage from exposing long-lived secrets, but those ephemeral scheduled jobs must be recreated after an App Service restart.
- For durable scheduled jobs without adding resources, set an App Service/local environment variable and reference it with `SCHEDULER_CLIENT_SECRET_ENV`, for example:
  - `SCHEDULER_CLIENT_SECRET_ENV=HACKOPS_SCHEDULER_CLIENT_SECRET`
  - `HACKOPS_SCHEDULER_CLIENT_SECRET=<secret value>`
- If you already have Key Vault, you can optionally use an existing secret URI via `SCHEDULER_CLIENT_SECRET_URI`; the app will resolve it at runtime with managed identity. This is optional and does not create a new Key Vault.
- Server-side confirmation is enforced for privileged operations: cleanup, read-only downgrade, RBAC assignment, TAP regeneration, and license assignment. Direct API calls must first receive a server-generated operation token, then replay the exact prefix/resource/subscription counts.
- The AI assistant is read-only by default. Mutation tools are hidden/blocked unless `CHATBOT_ENABLE_MUTATION_TOOLS=true`, and tool responses redact passwords, TAPs, secrets, and tokens.
- Rotate any service principal secrets that were previously persisted in `_scheduler/jobs.json` before this hardening.

### 2. Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
```

### 3. Configure

Copy `.env.example` → `.env` *or* `config.example.yaml` → `config.yaml` and fill in values.

Environment variables always override YAML.

```env
AZURE_TENANT_ID=...
AZURE_CLIENT_ID=...
AZURE_CLIENT_SECRET=...        # OR cert auth below
# AZURE_CERT_PATH=C:\secrets\graph-app.pem
# AZURE_CERT_THUMBPRINT=ABCDEF...
```

### 4. Smoke-test credentials

```powershell
onedrive-provisioner --config config.yaml list-users --limit 5
```

---

## CLI usage

```powershell
# Provision OneDrive for one user
onedrive-provisioner -c config.yaml provision alice@contoso.onmicrosoft.com

# Upload a folder to one user
onedrive-provisioner -c config.yaml upload alice@contoso.onmicrosoft.com `
    --source .\samples\payload --destination Onboarding

# Bulk run from a file, dry-run, 16 workers
onedrive-provisioner -c config.yaml bulk `
    --users-file .\samples\users.csv `
    --source .\samples\payload `
    --destination Onboarding `
    --dry-run --concurrency 16

# Bulk run for ALL enabled member users in tenant
onedrive-provisioner -c config.yaml bulk --all-users --source .\samples\payload --destination Onboarding
```

Reports are written to `./reports/onedrive-report-<UTCtimestamp>.{json,csv}` (configurable).

### Azure Blob source

```powershell
onedrive-provisioner -c config.yaml upload alice@contoso.onmicrosoft.com `
    --source "https://mystorage.blob.core.windows.net/onboarding/payload" `
    --destination Onboarding
```

Auth uses `DefaultAzureCredential` (managed identity, `az login`, env vars, …).

---

## MCP usage

Start the MCP server (stdio transport):

```powershell
$env:ONEDRIVE_PROVISIONER_CONFIG = "C:\path\to\config.yaml"
onedrive-provisioner-mcp
```

### Register with VS Code (`.vscode/mcp.json`)

```json
{
  "servers": {
    "onedrive-provisioner": {
      "command": "onedrive-provisioner-mcp",
      "env": {
        "ONEDRIVE_PROVISIONER_CONFIG": "C:/path/to/config.yaml"
      }
    }
  }
}
```

### Tools exposed

| Tool                  | Arguments                                                                                       | Returns |
|-----------------------|-------------------------------------------------------------------------------------------------|---------|
| `provision_onedrive`  | `user`                                                                                          | `UserResult` |
| `upload_folder`       | `user`, `source?`, `destination?`                                                               | `UserResult` |
| `bulk_setup`          | `users[]?`, `source?`, `destination?`, `dry_run?`, `concurrency?`, `all_users?`, `write_report?`| `BulkReport` (+ optional `report_files`) |
| `list_users`          | `limit?`                                                                                        | `[{upn,id}, …]` |

All responses are JSON text content — easy to consume programmatically.

---

## Output schema

```jsonc
// UserResult
{
  "user": "alice@contoso.onmicrosoft.com",
  "user_id": "abc-...",
  "drive_id": "b!...",
  "status": "success",   // success | failed | skipped | dry_run
  "message": null,
  "files": [
    { "path": "Onboarding/Welcome.txt", "size": 65, "status": "success", "message": null }
  ]
}

// BulkReport
{
  "total": 250, "succeeded": 248, "failed": 1, "skipped": 1,
  "results": [ /* UserResult[] */ ]
}
```

---

## Performance, scalability & throttling

- Concurrency capped by an `asyncio.Semaphore` (`execution.concurrency`, default 8).
- All Graph calls retry on `408 / 429 / 5xx` and transport errors with exponential backoff + jitter, capped at 60 s, honouring `Retry-After`.
- Chunked uploads are aligned to 320 KiB; configurable chunk size (default 10 MiB).
- For 1 000+ users, raise `concurrency` carefully (Graph per-app limits apply). Start with 8–16 and monitor logs for `graph.http_retry` events.

## Security best practices

- Never hardcode secrets — use env vars, Key Vault, GitHub Actions secrets, or managed identities.
- Prefer **certificate auth** for production (`cert_path` + `cert_thumbprint`).
- Grant only the three required application permissions; review consent regularly.
- Keep PEM keys readable only by the service account running the tool.
- Rotate secrets/certs on a schedule.

## Testing

```powershell
pytest -q
```

The test suite uses `httpx.MockTransport` and does not require any Azure credentials.

## Project layout

```
src/onedrive_provisioner/
├── auth/           # MSAL token provider (secret + cert)
├── graph/          # Async Graph client (retry, pagination, throttling)
├── onedrive/       # User resolver + drive provisioner
├── uploader/       # FileSource (local/blob) + simple/chunked uploader
├── mcp/            # MCP stdio server
├── orchestrator.py # Per-user pipeline + parallel bulk runner
├── reporting.py    # JSON/CSV report writers
├── cli.py          # Click CLI
├── config.py       # YAML + env config (pydantic)
├── logging_setup.py
└── models.py
```

## Limitations

- Targets `https://graph.microsoft.com/v1.0` (not legacy SharePoint REST).
- OneDrive must be enabled for users in the tenant (SharePoint admin → User profiles → Setup My Sites). The tool **triggers** provisioning by hitting `/users/{id}/drive` and waits with backoff, but cannot enable the licence/feature itself.
- Graph cannot force OneDrive provisioning if the user lacks a OneDrive-enabling licence (e.g., M365 E3/E5, OneDrive for Business plan).
