"""External identity rules (core/session/external_identity.py)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from core.session import external_identity as ei

SID = "11111111-2222-4333-8444-555555555555"


class TestNormalize:
    @pytest.mark.parametrize("raw, expected", [
        ("+302101234567", "+302101234567"),
        ("+30 210 123-4567", "+302101234567"),
        ("(210) 123.4567", "2101234567"),
        ("2101234567", "2101234567"),
        ("1001", "1001"),                    # an extension
        ("", ""),
        ("anonymous", ""),
        ("Restricted", ""),
        ("unknown", ""),
        ("abc+302101234567", ""),            # letters → withheld, never stripped
        ("+30210123456789012345678", ""),    # too long
        ("1", ""),                           # too short
        ("+", ""),
    ])
    def test_phone(self, raw, expected):
        assert ei.normalize_id(ei.PHONE, raw) == expected

    def test_slug_drops_the_plus(self):
        assert ei.slug_for("+302101234567") == "302101234567"
        assert ei.slug_for("2101234567") == "2101234567"


class TestResolve:
    def test_durable_caller(self):
        ident = ei.resolve(ei.PHONE, "+30 210 1234567", session_id=SID)
        assert ident.id == "+302101234567"
        assert ident.slug == "302101234567"
        assert ident.ephemeral is False
        assert ident.has_tree
        assert ident.claim == "phone:+302101234567"
        assert ident.label == "caller:+302101234567"
        assert ei.resolve(ei.PHONE, "+302101234567", session_id=SID, verified=True).label == \
            "caller-pin:+302101234567"

    def test_withheld_is_ephemeral(self):
        ident = ei.resolve(ei.PHONE, "anonymous", session_id=SID)
        assert ident.id == ""
        assert ident.ephemeral is True
        assert ident.slug == f"{ei.EPHEMERAL_DIRNAME}/{SID}"
        assert ident.claim == f"phone:ephemeral:{SID}"
        assert ident.label == "ephemeral"

    def test_remember_off_keeps_the_id_but_ephemeral_tree(self):
        ident = ei.resolve(ei.PHONE, "+302101234567", session_id=SID, remember=False)
        assert ident.id == "+302101234567"      # the prompt may still say who called
        assert ident.ephemeral is True
        assert ident.claim == f"phone:ephemeral:{SID}"

    def test_shared_has_no_tree(self):
        ident = ei.resolve(ei.PHONE, "+302101234567", session_id=SID, shared=True)
        assert ident.id == "" and ident.slug == "" and not ident.has_tree
        assert ident.claim == "phone:"
        assert ident.label == "shared"

    def test_session_id_must_be_a_uuid_for_ephemeral_trees(self):
        with pytest.raises(ValueError):
            ei.resolve(ei.PHONE, "", session_id="../../etc")
        # A durable caller never uses the session id as a path component.
        ei.resolve(ei.PHONE, "+302101234567", session_id="not-a-uuid")

    def test_channel_is_validated(self):
        with pytest.raises(ValueError):
            ei.resolve("Phone/../x", "+302101234567", session_id=SID)


class TestHomes:
    def test_home_and_claim_are_inverse(self, tmp_path):
        agent_dir = tmp_path / "agent"
        for raw in ("+302101234567", "anonymous"):
            ident = ei.resolve(ei.PHONE, raw, session_id=SID)
            home = ei.external_home(agent_dir, ident)
            assert home == ei.home_from_claim(agent_dir, ident.claim)
            assert home.is_relative_to(agent_dir / ei.EXTERNALS_DIRNAME / ei.PHONE)
        assert ei.external_home(agent_dir, ei.resolve(ei.PHONE, "x", session_id=SID, shared=True)) is None
        assert ei.home_from_claim(agent_dir, "phone:") is None

    @pytest.mark.parametrize("claim", [
        "", "phone", "phone:abc", "phone:ephemeral:nope", "ph one:+30210", "phone:../x",
    ])
    def test_malformed_claims_are_refused(self, tmp_path, claim):
        with pytest.raises(ValueError):
            ei.home_from_claim(tmp_path, claim)

    def test_parse_claim(self):
        assert ei.parse_claim("phone:+302101234567") == ("phone", "+302101234567", "")
        assert ei.parse_claim("phone:") == ("phone", "", "")
        assert ei.parse_claim(f"phone:ephemeral:{SID}") == ("phone", "", SID)

    def test_prune_only_ephemeral_trees(self, tmp_path):
        agent_dir = tmp_path / "agent"
        durable = ei.external_home(agent_dir, ei.resolve(ei.PHONE, "+302101234567", session_id=SID))
        ephemeral = ei.external_home(agent_dir, ei.resolve(ei.PHONE, "", session_id=SID))
        for p in (durable, ephemeral):
            (p / "context").mkdir(parents=True)
        assert ei.prune_ephemeral(durable) is False and durable.is_dir()
        assert ei.prune_ephemeral(ephemeral) is True and not ephemeral.exists()
        assert ei.prune_ephemeral(None) is False
        assert ei.prune_ephemeral(Path(str(uuid.uuid4()))) is False
