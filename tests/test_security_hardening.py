import os

from onedrive_provisioner.scheduler import HackScheduler
from onedrive_provisioner.chatbot.tool_executor import ToolExecutor
from onedrive_provisioner.security.operation_confirmation import OperationConfirmationError, OperationConfirmationStore
from onedrive_provisioner.security.scheduler_credentials import resolve_scheduler_credentials


class FakeBlob:
    def __init__(self):
        self.data = {}

    def read_json(self, path):
        return self.data.get(path)

    def write_json(self, path, payload):
        self.data[path] = payload


class FakeManager:
    def __init__(self):
        self._blob = FakeBlob()

    def list_hacks(self):
        return []

    def get_state(self, prefix):
        return {
            "prefix": prefix,
            "users": [
                {"userPrincipalName": "u1@example.com", "password": "pass", "tap": "tap-code"}
            ],
            "client_secret": "secret",
        }


def test_scheduler_persists_secret_reference_not_client_secret():
    manager = FakeManager()
    scheduler = HackScheduler(
        get_state_manager=lambda: manager,
        run_provision=lambda *args, **kwargs: None,
        run_cleanup=lambda *args, **kwargs: None,
    )

    job = scheduler.schedule_provision(
        "2026-05-01T09:00:00Z",
        {"prefix": "demo-", "domain": "contoso.onmicrosoft.com"},
        {"tenant_id": "tenant", "client_id": "client", "client_secret": "super-secret"},
    )

    persisted = manager._blob.data["_scheduler/jobs.json"]["jobs"][0]
    assert "client_secret" not in persisted["config"]
    assert persisted["config"]["client_secret_ref"]["type"] == "connection_id"
    assert job.to_dict()["config"]["client_secret_ref"]["id"].startswith("ephemeral-")
    tenant_id, client_id, secret = resolve_scheduler_credentials(persisted["config"])
    assert (tenant_id, client_id, secret) == ("tenant", "client", "super-secret")


def test_scheduler_uses_environment_secret_reference(monkeypatch):
    monkeypatch.setenv("MY_SCHEDULER_SECRET", "env-secret")
    manager = FakeManager()
    scheduler = HackScheduler(
        get_state_manager=lambda: manager,
        run_provision=lambda *args, **kwargs: None,
        run_cleanup=lambda *args, **kwargs: None,
    )

    scheduler.schedule_provision(
        "2026-05-01T09:00:00Z",
        {"prefix": "demo-", "domain": "contoso.onmicrosoft.com"},
        {
            "tenant_id": "tenant",
            "client_id": "client",
            "client_secret": "request-secret",
            "client_secret_ref": {"type": "environment_variable", "name": "MY_SCHEDULER_SECRET"},
        },
    )

    persisted = manager._blob.data["_scheduler/jobs.json"]["jobs"][0]
    assert "client_secret" not in persisted["config"]
    assert persisted["config"]["client_secret_ref"] == {"type": "environment_variable", "name": "MY_SCHEDULER_SECRET"}
    assert resolve_scheduler_credentials(persisted["config"])[2] == "env-secret"


def test_confirmation_token_binds_expected_values():
    store = OperationConfirmationStore(ttl_seconds=60)
    expected = {"prefix": "demo-", "resourceCount": 3, "subscriptionCount": 1}
    challenge = store.create("cleanup_hack", expected, operator="tester")

    store.validate(
        "cleanup_hack",
        expected,
        {
            "operationId": challenge["operationId"],
            "token": challenge["token"],
            "confirmText": challenge["confirmText"],
        },
        operator="tester",
    )


def test_confirmation_rejects_mismatched_counts():
    store = OperationConfirmationStore(ttl_seconds=60)
    challenge = store.create("cleanup_hack", {"prefix": "demo-", "resourceCount": 3, "subscriptionCount": 1})

    try:
        store.validate(
            "cleanup_hack",
            {"prefix": "demo-", "resourceCount": 4, "subscriptionCount": 1},
            {
                "operationId": challenge["operationId"],
                "token": challenge["token"],
                "confirmText": challenge["confirmText"],
            },
        )
    except OperationConfirmationError:
        return
    raise AssertionError("mismatched confirmation should fail")


def test_chatbot_tool_executor_is_read_only_by_default(monkeypatch):
    monkeypatch.delenv("CHATBOT_ENABLE_MUTATION_TOOLS", raising=False)
    executor = ToolExecutor(
        creds=("tenant", "client", "secret"),
        get_state_manager=lambda: FakeManager(),
        entra_sessions={},
        entra_lock=type("Lock", (), {"__enter__": lambda s: s, "__exit__": lambda *a: None})(),
        upload_jobs={},
        jobs_lock=type("Lock", (), {"__enter__": lambda s: s, "__exit__": lambda *a: None})(),
    )

    result = executor("assign_licenses", {"prefix": "demo-", "licenses": ["M365"]})

    assert "read-only" in result["error"]


def test_chatbot_tool_executor_redacts_hack_state(monkeypatch):
    monkeypatch.delenv("CHATBOT_ENABLE_MUTATION_TOOLS", raising=False)
    executor = ToolExecutor(
        creds=("tenant", "client", "secret"),
        get_state_manager=lambda: FakeManager(),
        entra_sessions={},
        entra_lock=type("Lock", (), {"__enter__": lambda s: s, "__exit__": lambda *a: None})(),
        upload_jobs={},
        jobs_lock=type("Lock", (), {"__enter__": lambda s: s, "__exit__": lambda *a: None})(),
    )

    result = executor("get_hack_state", {"prefix": "demo-"})

    assert result["client_secret"] == "[REDACTED]"
    assert result["users"][0]["password"] == "[REDACTED]"
    assert result["users"][0]["tap"] == "[REDACTED]"
