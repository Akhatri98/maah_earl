"""Environment / credential loading.

Deliberately stdlib-only: config must work before `pip install -r
requirements.txt` has run, and the credential smoke tests need it early.
Values come from the process environment first, then `.env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def load_env(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Parse a .env file into a dict and merge it into os.environ."""
    path = path or ENV_PATH
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        values[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def require(name: str) -> str:
    """Fetch a credential, failing with a useful message if it is missing."""
    load_env()
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to {ENV_PATH} (see .env.example)."
        )
    return value


@dataclass(frozen=True)
class OnshapeConfig:
    access_key: str
    secret_key: str
    base_url: str
    document_id: str
    workspace_id: str

    @classmethod
    def from_env(cls) -> OnshapeConfig:
        load_env()
        return cls(
            access_key=require("ONSHAPE_ACCESS_KEY"),
            secret_key=require("ONSHAPE_SECRET_KEY"),
            base_url=os.environ.get(
                "ONSHAPE_BASE_URL", "https://cad.onshape.com"
            ).rstrip("/"),
            document_id=require("ONSHAPE_DOCUMENT_ID"),
            workspace_id=require("ONSHAPE_WORKSPACE_ID"),
        )


@dataclass(frozen=True)
class SkyCivConfig:
    username: str
    key: str
    # Port-qualified; the bare host does NOT serve the API.
    base_url: str = "https://api.skyciv.com:8085/v3"

    @classmethod
    def from_env(cls) -> SkyCivConfig:
        load_env()
        return cls(
            username=require("SKYCIV_API_USERNAME"),
            key=require("SKYCIV_API_KEY"),
        )


@dataclass(frozen=True)
class GmailConfig:
    """Send-only Gmail credentials (OAuth refresh token flow).

    The refresh token is minted once, out of band, for the
    `https://www.googleapis.com/auth/gmail.send` scope only, so the pipeline
    can post a message but never read a mailbox.
    """

    client_id: str
    client_secret: str
    refresh_token: str
    sender: str
    recipient: str
    token_url: str = "https://oauth2.googleapis.com/token"
    send_url: str = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

    @classmethod
    def from_env(cls) -> GmailConfig:
        load_env()
        return cls(
            client_id=require("GMAIL_CLIENT_ID"),
            client_secret=require("GMAIL_CLIENT_SECRET"),
            refresh_token=require("GMAIL_REFRESH_TOKEN"),
            sender=require("GMAIL_SENDER_ADDRESS"),
            recipient=require("GMAIL_NOTIFY_RECIPIENT"),
        )

    @classmethod
    def configured(cls) -> bool:
        """True when every Gmail variable is set -- so a script can choose
        between sending and writing an .eml without raising."""
        load_env()
        return all(
            os.environ.get(name)
            for name in (
                "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN",
                "GMAIL_SENDER_ADDRESS", "GMAIL_NOTIFY_RECIPIENT",
            )
        )


def skyciv_report_dir() -> Path | None:
    """Where escalation saves SkyCiv report PDFs, or None when unset."""
    load_env()
    raw = os.environ.get("SKYCIV_REPORT_DIR", "")
    return (PROJECT_ROOT / raw) if raw and not Path(raw).is_absolute() else (Path(raw) if raw else None)


def safety_factor_threshold() -> float:
    load_env()
    return float(os.environ.get("SAFETY_FACTOR_THRESHOLD", "1.0"))
