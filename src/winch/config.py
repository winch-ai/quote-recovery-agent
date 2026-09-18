"""Runtime configuration. Values come from the environment, loaded by
scripts/with_env.sh into the process — never read from a file here, and never
logged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(Exception):
    """A required setting is missing. Raised at boot, never mid-request."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


@dataclass(frozen=True)
class Settings:
    # Azure OpenAI
    azure_endpoint: str
    azure_api_key: str = field(repr=False)          # never in a repr or a log
    azure_deployment: str
    azure_api_version: str = "2024-10-21"

    # Meta WhatsApp
    meta_phone_number_id: str = ""
    meta_access_token: str = field(default="", repr=False)
    meta_app_secret: str = field(default="", repr=False)
    meta_verify_token: str = field(default="", repr=False)
    meta_graph_version: str = "v23.0"

    database_url: str = field(default="", repr=False)   # contains a password

    # Cloud Run auth is per-service, not per-path. The service must be public so
    # Meta can reach the webhook, which means /internal/tick is public too. This
    # shared secret is what keeps it from being a free DoS handle.
    tick_secret: str = field(default="", repr=False)

    # The single contractor this instance serves. v1 is deliberately not
    # multi-tenant; see docs/DESIGN.md.
    contractor_wa_id: str = ""
    contractor_first_name: str = ""
    contractor_business_name: str = ""
    contractor_timezone: str = "Europe/London"

    # Shown on the public /privacy page, which Meta requires before an app can
    # be switched from Development to Live.
    privacy_contact_email: str = ""
    privacy_operator_name: str = "the operator of this service"

    @classmethod
    def from_env(cls) -> "Settings":
        raw = os.environ.get("LLM_MODEL", "")
        deployment = raw.split(":", 1)[-1] if raw else ""
        if not deployment:
            raise ConfigError("LLM_MODEL is not set")
        return cls(
            azure_endpoint=_require("AZURE_OPENAI_ENDPOINT").rstrip("/"),
            azure_api_key=_require("AZURE_OPENAI_API_KEY"),
            azure_deployment=deployment,
            azure_api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            meta_phone_number_id=os.environ.get("META_PHONE_NUMBER_ID", ""),
            meta_access_token=os.environ.get("META_ACCESS_TOKEN", ""),
            meta_app_secret=os.environ.get("META_APP_SECRET", ""),
            meta_verify_token=os.environ.get("META_VERIFY_TOKEN", ""),
            meta_graph_version=os.environ.get("META_GRAPH_VERSION", "v23.0"),
            database_url=os.environ.get("DATABASE_URL", ""),
            tick_secret=os.environ.get("TICK_SECRET", ""),
            contractor_wa_id=os.environ.get("CONTRACTOR_WA_ID", ""),
            contractor_first_name=os.environ.get("CONTRACTOR_FIRST_NAME", ""),
            contractor_business_name=os.environ.get("CONTRACTOR_BUSINESS_NAME", ""),
            contractor_timezone=os.environ.get("CONTRACTOR_TIMEZONE", "Europe/London"),
            privacy_contact_email=os.environ.get("PRIVACY_CONTACT_EMAIL", ""),
            privacy_operator_name=os.environ.get(
                "PRIVACY_OPERATOR_NAME", "the operator of this service"),
        )
