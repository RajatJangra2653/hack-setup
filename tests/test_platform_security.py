"""Tests for platform_core security module."""

import pytest
from platform_core.security import (
    redact_dict,
    validate_prefix,
    require_confirmation,
    SafeguardError,
)


def test_redact_passwords():
    data = {"username": "alice", "password": "secret123", "email": "a@b.com"}
    result = redact_dict(data)
    assert result["username"] == "alice"
    assert result["password"] == "***REDACTED***"
    assert result["email"] == "a@b.com"


def test_redact_nested():
    data = {"user": {"name": "bob", "tap_code": "12345"}}
    result = redact_dict(data)
    assert result["user"]["tap_code"] == "***REDACTED***"


def test_redact_jwt():
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    data = {"authorization": token}
    result = redact_dict(data)
    assert "***REDACTED_TOKEN***" in str(result["authorization"])


def test_validate_prefix_normalization():
    assert validate_prefix("HACK") == "hack-"
    assert validate_prefix("hack-") == "hack-"
    assert validate_prefix("  test  ") == "test-"


def test_validate_prefix_invalid():
    with pytest.raises(ValueError):
        validate_prefix("")
    with pytest.raises(ValueError):
        validate_prefix("--bad")


def test_require_confirmation_raises():
    with pytest.raises(SafeguardError):
        require_confirmation("delete", "hack-1")


def test_require_confirmation_force():
    # Should not raise
    require_confirmation("delete", "hack-1", force=True)
