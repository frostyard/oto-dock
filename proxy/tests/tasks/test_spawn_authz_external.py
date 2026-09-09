"""External principals never delegate and never read the delegation roster."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from auth.providers import UserContext
from core.session.visibility import nouser_read_targets
from services.delegation.spawn_authz import authorize_spawn


def _external() -> UserContext:
    return UserContext(sub="session:abc", email="", name="", role="agent",
                       is_api_key=True, agent="support", external_claim="phone:+3021")


def test_spawn_is_refused_before_any_lookup():
    with pytest.raises(HTTPException) as exc:
        authorize_spawn(_external(), target_agent="support", requested_scope="agent")
    assert exc.value.status_code == 403
    assert "external routes" in exc.value.detail


def test_roster_reads_are_empty():
    assert nouser_read_targets(_external()) == set()
