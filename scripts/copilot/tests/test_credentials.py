"""SDK-independent payer identity, expiry, and authentication channel contract."""

from dataclasses import FrozenInstanceError, replace
from enum import Enum
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))

from core.layers.copilot.credentials import (  # noqa: E402
    AccountScopeKind, CopilotAccountScope, CopilotCredential,
    CredentialKind, CredentialUnavailableError,
)


def credential(**overrides):
    values = dict(account_id="account", principal_id="principal", revision="opaque-revision",
                  kind=CredentialKind.USER_TOKEN, token="ghu_privateMaterial", expires_at=None)
    return CopilotCredential(**(values | overrides))


class CredentialTests(unittest.TestCase):
    def test_user_shapes_route_only_to_explicit_sdk_parameter(self):
        for token in ("ghu_privateMaterial", "gho_privateMaterial", "github_pat_private_material"):
            with self.subTest(prefix=token.split("_", 1)[0]):
                item = credential(token=token)
                with patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "ambient",
                                             "GH_TOKEN": "other"}, clear=True):
                    self.assertEqual(item.runtime_auth(), (token, {}))

    def test_installation_routes_only_to_inference_environment(self):
        item = credential(kind=CredentialKind.INSTALLATION_TOKEN, token="ghs_privateMaterial", expires_at=100)
        token, env = item.runtime_auth()
        self.assertIsNone(token)
        self.assertEqual(env, {"COPILOT_GITHUB_TOKEN": "ghs_privateMaterial"})
        env.clear()
        self.assertEqual(item.runtime_auth()[1], {"COPILOT_GITHUB_TOKEN": "ghs_privateMaterial"})

    def test_incompatible_or_malformed_tokens_never_enter_wrong_channel(self):
        invalid = ["", " ", "ghu_", "gho_", "github_pat_", "ghp_classic", "ghr_refresh",
                   "plain-token", "ghu_has space", "ghu_newline\n", " ghu_padded", "ghu_é", None, 12]
        for token in invalid:
            with self.subTest(token_type=type(token).__name__), self.assertRaises(CredentialUnavailableError):
                credential(token=token)
        with self.assertRaises(CredentialUnavailableError):
            credential(token="ghs_installation")
        for token in ("ghu_user", "gho_oauth", "github_pat_fine", "ghs_", "ghp_classic"):
            with self.subTest(prefix=token.split("_", 1)[0]), self.assertRaises(CredentialUnavailableError):
                credential(kind=CredentialKind.INSTALLATION_TOKEN, token=token, expires_at=100)

    def test_expiry_requires_finite_number_and_installation_requires_known_expiry(self):
        for expiry in (True, False, "100", float("nan"), float("inf"), -float("inf"), 10**1000):
            with self.subTest(expiry_type=type(expiry).__name__), self.assertRaises(CredentialUnavailableError):
                credential(expires_at=expiry)
        with self.assertRaises(CredentialUnavailableError):
            credential(kind=CredentialKind.INSTALLATION_TOKEN, token="ghs_private")
        # A stored expired credential is representable, but cannot be used.
        with self.assertRaises(CredentialUnavailableError):
            credential(expires_at=-1).ensure_usable(now=0)

    def test_expiry_and_runway_equality_are_not_usable(self):
        item = credential(expires_at=100)
        item.ensure_usable(now=99.5)
        item.ensure_usable(now=90, min_runway=9.5)
        for now, runway in ((100, 0), (101, 0), (90, 10), (90, 11)):
            with self.subTest(now=now, runway=runway), self.assertRaises(CredentialUnavailableError):
                item.ensure_usable(now, runway)

    def test_unknown_user_expiry_is_not_positive_lifetime_proof(self):
        item = credential()
        self.assertIsNone(item.expires_at)
        item.ensure_usable(now=100)
        with self.assertRaises(CredentialUnavailableError):
            item.ensure_usable(now=100, min_runway=0.01)

    def test_invalid_clock_and_runway_are_sanitized(self):
        for now, runway in ((True, 0), ("100", 0), (float("nan"), 0), (0, -1),
                            (0, True), (0, float("inf")), (10**1000, 0), (1e308, 1e308)):
            with self.subTest(now_type=type(now).__name__), self.assertRaises(CredentialUnavailableError):
                credential().ensure_usable(now, runway)

    def test_strict_ids_and_enum_types(self):
        for name in ("account_id", "principal_id", "revision"):
            for value in (None, "", " ", " padded", "line\nbreak", 1):
                with self.subTest(name=name), self.assertRaises(CredentialUnavailableError):
                    credential(**{name: value})
        class OtherKind(Enum):
            USER_TOKEN = "user_token"
        for kind in ("user_token", OtherKind.USER_TOKEN, None, 1):
            with self.subTest(kind_type=type(kind).__name__), self.assertRaises(CredentialUnavailableError):
                credential(kind=kind)
        self.assertEqual(credential(revision="opaque:not-a-uuid").revision, "opaque:not-a-uuid")

    def test_frozen_repr_and_errors_do_not_disclose_token(self):
        secret = "ghu_superSecretMaterial"
        item = credential(token=secret)
        self.assertNotIn(secret, repr(item))
        self.assertNotIn(secret, str(item))
        with self.assertRaises(FrozenInstanceError):
            item.token = "replacement"
        for operation in (lambda: replace(item, kind=CredentialKind.INSTALLATION_TOKEN),
                          lambda: replace(item, expires_at=1).ensure_usable(2)):
            with self.assertRaises(CredentialUnavailableError) as captured:
                operation()
            self.assertNotIn(secret, str(captured.exception))
            self.assertNotIn(secret, repr(captured.exception))
            self.assertIsNone(captured.exception.__context__)


class ScopeTests(unittest.TestCase):
    def test_factories_are_explicit_frozen_and_distinct(self):
        personal = CopilotAccountScope.personal("user-sub")
        platform = CopilotAccountScope.platform()
        self.assertEqual(personal, CopilotAccountScope(AccountScopeKind.PERSONAL, "user-sub"))
        self.assertEqual(platform, CopilotAccountScope(AccountScopeKind.PLATFORM, None))
        self.assertNotEqual(personal, platform)
        with self.assertRaises(FrozenInstanceError):
            personal.user_sub = "other-user"

    def test_empty_personal_scope_never_falls_back_to_platform(self):
        for user_sub in ("", " ", None, " padded", "line\nbreak", False):
            with self.subTest(value_type=type(user_sub).__name__), self.assertRaises(ValueError):
                CopilotAccountScope.personal(user_sub)

    def test_platform_requires_none_and_scope_enum_is_strict(self):
        for user_sub in ("", "user-sub", False):
            with self.subTest(value_type=type(user_sub).__name__), self.assertRaises(ValueError):
                CopilotAccountScope(AccountScopeKind.PLATFORM, user_sub)
        for kind in ("personal", "platform", CredentialKind.USER_TOKEN, None):
            with self.subTest(kind_type=type(kind).__name__), self.assertRaises(ValueError):
                CopilotAccountScope(kind, None)


if __name__ == "__main__":
    unittest.main()
