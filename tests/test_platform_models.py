"""Tests for platform_core models."""

import pytest
from platform_core.models.hack import HackEnvironment, HackConfig, HackStatus


def test_hack_status_transitions():
    config = HackConfig(name="Test Hack", prefix="test-", domain="contoso.com")
    env = HackEnvironment(config=config)
    assert env.status == HackStatus.DRAFT

    env.transition(HackStatus.PROVISIONING)
    assert env.status == HackStatus.PROVISIONING

    env.transition(HackStatus.ACTIVE)
    assert env.status == HackStatus.ACTIVE


def test_invalid_transition():
    config = HackConfig(name="Test", prefix="t-", domain="x.com")
    env = HackEnvironment(config=config)
    # Cannot go from DRAFT to ACTIVE directly
    with pytest.raises(ValueError, match="Cannot transition"):
        env.transition(HackStatus.ACTIVE)


def test_hack_config_defaults():
    config = HackConfig(name="Test", prefix="t-", domain="x.com")
    assert config.user_count == 0
    assert config.teams == []
    assert config.licenses == []
