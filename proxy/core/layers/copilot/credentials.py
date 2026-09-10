"""Explicit Copilot payer scope and authentication material, without SDK or I/O.

Token shapes select an authentication channel; they do not prove entitlement,
identity, revocation status, or server acceptance. Callers must check usability
against their clock before launching a runtime. Unknown user-token expiry is
unverified and cannot establish a positive minimum lifetime.
"""

from dataclasses import dataclass, field
from enum import Enum
import math
import re


class CredentialUnavailableError(ValueError):
    """Credential material cannot satisfy the requested local contract."""


class CredentialKind(Enum):
    USER_TOKEN = "user_token"
    INSTALLATION_TOKEN = "installation_token"


class AccountScopeKind(Enum):
    PERSONAL = "personal"
    PLATFORM = "platform"


def _identity(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip() and value.isprintable()


def _finite(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class CopilotAccountScope:
    kind: AccountScopeKind
    user_sub: str | None

    def __post_init__(self) -> None:
        if (not isinstance(self.kind, AccountScopeKind)
                or (self.kind is AccountScopeKind.PERSONAL and not _identity(self.user_sub))
                or (self.kind is AccountScopeKind.PLATFORM and self.user_sub is not None)):
            raise ValueError("Invalid Copilot account scope")

    @classmethod
    def personal(cls, user_sub: str) -> "CopilotAccountScope":
        return cls(AccountScopeKind.PERSONAL, user_sub)

    @classmethod
    def platform(cls) -> "CopilotAccountScope":
        return cls(AccountScopeKind.PLATFORM, None)


@dataclass(frozen=True)
class CopilotCredential:
    account_id: str
    principal_id: str
    revision: str
    kind: CredentialKind
    token: str = field(repr=False)
    expires_at: float | int | None = None

    def __post_init__(self) -> None:
        if (not all(_identity(value) for value in (self.account_id, self.principal_id, self.revision))
                or not isinstance(self.kind, CredentialKind)
                or not isinstance(self.token, str)
                or (self.expires_at is not None and not _finite(self.expires_at))):
            raise CredentialUnavailableError("Invalid Copilot credential")
        pattern = (r"(?:gh[ou]_|github_pat_)[A-Za-z0-9_]+"
                   if self.kind is CredentialKind.USER_TOKEN else r"ghs_[A-Za-z0-9_]+")
        if (re.fullmatch(pattern, self.token) is None
                or (self.kind is CredentialKind.INSTALLATION_TOKEN and self.expires_at is None)):
            raise CredentialUnavailableError("Invalid Copilot credential")

    def ensure_usable(self, now: float, min_runway: float = 0) -> None:
        """Validate local expiry only; equality with the deadline is expired.

        ``expires_at=None`` permits user tokens with unknown lifetime only when
        no positive runway is required. It never verifies remote validity.
        """
        if (not _finite(now) or not _finite(min_runway) or min_runway < 0
                or not _finite(now + min_runway)):
            raise CredentialUnavailableError("Invalid Copilot credential lifetime requirement")
        if self.expires_at is None:
            if min_runway > 0:
                raise CredentialUnavailableError("Copilot credential lifetime is unknown")
        elif self.expires_at <= now + min_runway:
            raise CredentialUnavailableError("Copilot credential lifetime is insufficient")

    def runtime_auth(self) -> tuple[str | None, dict[str, str]]:
        """Return an explicit channel; caller must first call ensure_usable.

        No ambient environment is read or merged. The returned environment is
        fresh, and must be installed only in the inference runtime boundary.
        """
        if self.kind is CredentialKind.USER_TOKEN:
            return self.token, {}
        return None, {"COPILOT_GITHUB_TOKEN": self.token}
