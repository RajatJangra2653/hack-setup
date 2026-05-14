"""OpenAI function-calling tool definitions for the chatbot agent."""
from __future__ import annotations

import os

# ── Tool definitions (maps to internal API calls) ──
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_saved_hacks",
            "description": "List all hacks that have been provisioned and saved to blob storage. Returns prefix, hackName, domain, totalUsers, lastUpdated.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hack_state",
            "description": "Get full state (users, groups, config, summary) for a specific hack by its prefix.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix (e.g. 'nyc-esri-gcc')"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hack_users_summary",
            "description": "Get a summary of users in a hack: total count, created/failed/existing breakdown, admin count, list of UPNs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_hack_report",
            "description": "Generate a saved hack report with user/admin counts, licenses assigned, and cost allocation by subscription/team/user. By default it auto-fetches actual Azure costs for every subscription on the hack (or every accessible sub if none recorded). Pass fetchSubscriptionCosts=false to skip the Azure call and only use manual subscriptionCosts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                    "currency": {"type": "string", "description": "Currency code, default USD"},
                    "fetchSubscriptionCosts": {
                        "type": "boolean",
                        "description": "When true (default), call Azure Cost Management to get actual costs for the hack's subscriptions. Set false only if the user explicitly wants manual-input-only mode.",
                    },
                    "forceRefresh": {
                        "type": "boolean",
                        "description": "When true, bypass the 15-minute cost cache and re-query Azure. Use only if the user explicitly asks for fresh data.",
                    },
                    "startDate": {
                        "type": "string",
                        "description": "ISO YYYY-MM-DD start of the cost window. Defaults to the hack's creation date.",
                    },
                    "endDate": {
                        "type": "string",
                        "description": "ISO YYYY-MM-DD end of the cost window. Defaults to today (UTC).",
                    },
                    "budget": {
                        "type": "number",
                        "description": "Optional Azure-only budget to compare actual Azure spend against.",
                    },
                    "licenseUnitCosts": {
                        "type": "object",
                        "description": "Optional map of license/SKU name to monthly unit cost",
                        "additionalProperties": {"type": "number"},
                    },
                    "subscriptionCosts": {
                        "type": "array",
                        "description": "Optional manual subscription cost overrides. Each item may include subscriptionId, cost, and team.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "subscriptionId": {"type": "string"},
                                "cost": {"type": "number"},
                                "team": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_provisioning_sessions",
            "description": "List recent provisioning sessions (in-memory). Shows session IDs, status, config, timing.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_session_status",
            "description": "Get detailed status of a specific provisioning session by session ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "The provisioning session UUID"},
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_tenant_info",
            "description": "Detect tenant domain, TAP max lifetime, and available license SKUs. Requires SPN credentials.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_preflight_check",
            "description": "Run pre-flight validation to check if provisioning can proceed. Validates domain, licenses, permissions, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Hack prefix"},
                    "domain": {"type": "string", "description": "UPN domain"},
                    "teams": {"type": "integer", "description": "Number of teams"},
                    "usersPerTeam": {"type": "integer", "description": "Users per team"},
                    "adminUsers": {"type": "integer", "description": "Number of admin users"},
                    "mode": {"type": "string", "enum": ["team", "flat"], "description": "Provisioning mode"},
                },
                "required": ["prefix", "domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "provision_users",
            "description": "Provision users for a hack. Creates Entra ID users, assigns licenses, generates TAPs, and creates groups. Runs synchronously and returns the full report with user details (UPN, password, TAP, licenses). Always present the results in a clear table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Hack prefix (e.g. 'nyc-esri-gcc-')"},
                    "domain": {"type": "string", "description": "UPN domain (e.g. 'WWPS319.onmicrosoft.com')"},
                    "teams": {"type": "integer", "description": "Number of teams (default 2)"},
                    "usersPerTeam": {"type": "integer", "description": "Users per team (default 5)"},
                    "adminUsers": {"type": "integer", "description": "Admin users (default 1)"},
                    "mode": {"type": "string", "enum": ["team", "flat"], "description": "Provisioning mode"},
                    "licenses": {"type": "array", "items": {"type": "string"}, "description": "License SKU names to assign"},
                    "assignLicensesToAdmins": {"type": "boolean", "description": "Set true to assign selected licenses to admin users also"},
                    "hackName": {"type": "string", "description": "Human-friendly hack name"},
                    "dryRun": {"type": "boolean", "description": "If true, simulate without creating anything"},
                },
                "required": ["prefix", "domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "regenerate_tap",
            "description": "Regenerate Temporary Access Pass (TAP) for users in an existing hack stored in blob storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                    "tapLifetime": {"type": "integer", "description": "TAP lifetime in minutes (default 120)"},
                    "users": {"type": "array", "items": {"type": "string"}, "description": "Specific UPNs to regenerate TAP for (omit for all)"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_licenses",
            "description": "Assign additional licenses to users in an existing hack stored in blob storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                    "licenses": {"type": "array", "items": {"type": "string"}, "description": "License SKU part numbers to assign"},
                    "users": {"type": "array", "items": {"type": "string"}, "description": "Specific UPNs (omit for all non-admin users, or all users when includeAdmins is true)"},
                    "includeAdmins": {"type": "boolean", "description": "Set true to include admin users in license assignment"},
                },
                "required": ["prefix", "licenses"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_hack_resources",
            "description": "Discover existing Entra ID users and groups that match a hack prefix.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix to search for"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_upload_jobs",
            "description": "List all OneDrive file upload jobs with their status.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cleanup_hack",
            "description": "Privileged mutation: delete all Entra ID users and groups for a hack, then archive saved state. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix to clean up"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_hack_state",
            "description": "Privileged mutation: delete only saved state from blob storage for a hack prefix. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_admin_guide",
            "description": "Generate a professional Admin/Trainer Guide Word document (.docx) for a saved hack. This is a read-only artifact generation action. Returns a download URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix to generate the guide for"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_hack_end_date",
            "description": "Privileged mutation: set an auto-cleanup end date for a hack. Scheduled cleanup deletes users/groups/RBAC and archives blob state. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                    "end_date": {"type": "string", "description": "ISO datetime for auto-cleanup (e.g. '2025-02-15T18:00:00Z')"},
                    "subscription_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Azure subscription IDs to remove RBAC role assignments from during cleanup (optional)",
                    },
                },
                "required": ["prefix", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_hack_provision",
            "description": "Schedule a hack to be provisioned at a future date/time. The scheduler will automatically create users, groups, and licenses at the specified time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scheduled_at": {"type": "string", "description": "ISO datetime to start provisioning (e.g. '2025-02-01T09:00:00Z')"},
                    "config": {
                        "type": "object",
                        "description": "Provisioning config (same as provision-users): prefix, hackName, domain, totalUsers, teamsCount, mode, licenses, tapLifetimeMinutes, etc.",
                    },
                },
                "required": ["scheduled_at", "config"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scheduled_jobs",
            "description": "List all scheduled jobs (both provisioning and cleanup). Shows job type, prefix, scheduled time, and status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status: pending, running, completed, failed, cancelled. Omit for all.", "enum": ["pending", "running", "completed", "failed", "cancelled"]},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_scheduled_job",
            "description": "Cancel a pending scheduled job by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The job ID to cancel"},
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enable_github_access",
            "description": "Privileged mutation: add hack users to GitHub-EMU-backed Entra groups and trigger on-demand sync. Uses a separate broker tenant (configured server-side). Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix — users are resolved from saved state"},
                    "withCopilot": {"type": "boolean", "description": "Include GitHub Copilot group (default false)"},
                    "withGhas": {"type": "boolean", "description": "Include GHAS group (default false)"},
                    "includeAdmins": {"type": "boolean", "description": "Include admin users (default false)"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "disable_github_access",
            "description": "Privileged mutation: remove hack users from GitHub-EMU groups and trigger sync for deprovisioning. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix — users are resolved from saved state"},
                    "withCopilot": {"type": "boolean", "description": "Also remove from Copilot group (default false)"},
                    "withGhas": {"type": "boolean", "description": "Also remove from GHAS group (default false)"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_rbac_permissions",
            "description": "Privileged mutation: assign an Azure RBAC role (Reader/Contributor/Owner) on subscriptions to hack groups or users. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix to discover groups/users"},
                    "subscriptionIds": {"type": "array", "items": {"type": "string"}, "description": "Azure subscription IDs to assign the role on"},
                    "role": {"type": "string", "enum": ["Reader", "Contributor", "Owner"], "description": "RBAC role to assign (default Reader)"},
                    "targetScope": {"type": "string", "enum": ["teams", "teams-admins", "admins", "users"], "description": "Which principals to assign to (default teams)"},
                },
                "required": ["prefix", "subscriptionIds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_readonly",
            "description": "Privileged mutation: switch hack to read-only mode — removes Owner/Contributor role assignments and grants only Reader access on listed subscriptions. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                    "subscriptionIds": {"type": "array", "items": {"type": "string"}, "description": "Subscription IDs to apply read-only on (required)"},
                    "mode": {"type": "string", "enum": ["team", "flat"], "description": "team = operate on groups, flat = operate on individual users (default team)"},
                },
                "required": ["prefix", "subscriptionIds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_user_password",
            "description": "Privileged mutation: reset passwords for users in a saved hack. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                    "users": {"type": "array", "items": {"type": "string"}, "description": "Specific UPNs to reset (omit for all non-admin users)"},
                    "password": {"type": "string", "description": "Custom password (omit for random per-user passwords)"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_hack_dates",
            "description": "Privileged mutation: update lifecycle dates (hack start, hack day, read-only, delete/end) for a saved hack and reschedule lifecycle automation. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                    "hackStartDate": {"type": "string", "description": "ISO datetime for hack start (optional)"},
                    "hackDate": {"type": "string", "description": "ISO datetime for hack day (optional)"},
                    "readonlyDate": {"type": "string", "description": "ISO datetime for read-only mode (optional)"},
                    "deleteDate": {"type": "string", "description": "ISO datetime for auto-cleanup/delete (required)"},
                    "subscriptionIds": {"type": "array", "items": {"type": "string"}, "description": "Subscription IDs for RBAC cleanup (optional)"},
                },
                "required": ["prefix", "deleteDate"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repair_groups",
            "description": "Privileged mutation: verify and repair group memberships for all users in a hack — re-adds any missing memberships. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repair_licenses",
            "description": "Privileged mutation: re-assign licenses to users in a hack who are missing expected licenses. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_subscription_cost",
            "description": (
                "Fetch the actual Azure cost for ONE subscription, identified "
                "by GUID or display name (case-insensitive substring). "
                "Returns total cost, currency, and date range. Defaults to "
                "the last 30 days. Requires Reader + Cost Management Reader "
                "on the subscription."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subscription": {
                        "type": "string",
                        "description": "Subscription GUID (e.g. 77086d22-...) OR display name / partial name (e.g. 'CopilotLabs DS - 1015').",
                    },
                    "startDate": {
                        "type": "string",
                        "description": "ISO start date YYYY-MM-DD (inclusive). Defaults to 30 days ago.",
                    },
                    "endDate": {
                        "type": "string",
                        "description": "ISO end date YYYY-MM-DD (inclusive). Defaults to today (UTC).",
                    },
                },
                "required": ["subscription"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "expand_hack",
            "description": (
                "Expand an EXISTING hack by adding more teams, more participants per team, "
                "and/or more admins. Indices continue from the current max — existing users, "
                "passwords, TAPs, licenses, and group memberships are NEVER touched. "
                "Example: a hack with 5 teams × 4 participants and 5 admins, called with "
                "addTeams=1, addParticipantsPerTeam=1, addAdmins=2 will create team t06 (with "
                "u01..u05), add u05 to t01..t05, and create admin06 + admin07. "
                "This is a MUTATION tool — only available when CHATBOT_ENABLE_MUTATION_TOOLS is set. "
                "Returns a session_id; poll get_session_status to monitor progress."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Hack prefix (e.g. 'fbi-cjis-')"},
                    "addTeams": {"type": "integer", "description": "Number of new teams to add (>=0). Default 0.", "minimum": 0},
                    "addParticipantsPerTeam": {"type": "integer", "description": "Extra participants to add to EVERY team (existing + new). Default 0.", "minimum": 0},
                    "addAdmins": {"type": "integer", "description": "Number of new admin users to add. Default 0.", "minimum": 0},
                    "dryRun": {"type": "boolean", "description": "When true, simulate without creating users."},
                },
                "required": ["prefix"],
            },
        },
    },
]

READ_ONLY_TOOL_NAMES = {
    "list_saved_hacks",
    "get_hack_state",
    "get_hack_users_summary",
    "generate_hack_report",
    "get_subscription_cost",
    "get_provisioning_sessions",
    "get_session_status",
    "detect_tenant_info",
    "run_preflight_check",
    "discover_hack_resources",
    "list_upload_jobs",
    "list_scheduled_jobs",
    "generate_admin_guide",
}

if os.environ.get("CHATBOT_ENABLE_MUTATION_TOOLS", "").strip().lower() not in {"1", "true", "yes", "on"}:
    TOOLS = [tool for tool in TOOLS if tool["function"]["name"] in READ_ONLY_TOOL_NAMES]
