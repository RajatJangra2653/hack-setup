from onedrive_provisioner.storage.state_manager import HackStateManager


class FakeBlobClient:
    def __init__(self):
        self.data = {}

    def read_json(self, path):
        return self.data.get(path)

    def write_json(self, path, payload):
        self.data[path] = payload

    def list_blobs(self, prefix=""):
        return [path for path in self.data if path.startswith(prefix)]

    def delete_blob(self, path):
        return self.data.pop(path, None) is not None


def test_archive_state_moves_active_state_and_strips_credentials():
    blob = FakeBlobClient()
    mgr = HackStateManager(blob)
    blob.data["demo/state.json"] = {
        "prefix": "demo-",
        "hackName": "Demo",
        "domain": "contoso.onmicrosoft.com",
        "totalUsers": 1,
        "users": [
            {
                "userPrincipalName": "demo-u01@contoso.onmicrosoft.com",
                "password": "Secret!23",
                "tap": "12345678",
                "licenses": ["M365_E3"],
            }
        ],
    }

    archived = mgr.archive_state("demo-", cleanup_result={"users": [{"status": "deleted"}]})

    assert archived is True
    assert "demo/state.json" not in blob.data
    archive = blob.data["archive/demo/state.json"]
    assert archive["isArchived"] is True
    assert archive["lifecycleStatus"] == "archived"
    assert archive["cleanupResult"]["users"][0]["status"] == "deleted"
    assert archive["users"][0]["licenses"] == ["M365_E3"]
    assert "password" not in archive["users"][0]
    assert "tap" not in archive["users"][0]
    assert archive["users"][0]["credentialsArchived"] is True
    assert mgr.get_state("demo-")["isArchived"] is True
    assert mgr.list_hacks() == []
    archived_list = mgr.list_archived_hacks()
    assert len(archived_list) == 1
    assert archived_list[0]["prefix"] == "demo-"
    assert archived_list[0]["archived"] is True


def test_list_versions_includes_archive_versions():
    blob = FakeBlobClient()
    mgr = HackStateManager(blob)
    blob.data["demo/state_2024.json"] = {"prefix": "demo-"}
    blob.data["archive/demo/state_2025.json"] = {"prefix": "demo-", "isArchived": True}

    versions = mgr.list_versions("demo-")

    assert "demo/state_2024.json" in versions
    assert "archive/demo/state_2025.json" in versions
