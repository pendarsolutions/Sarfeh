from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]
#: scripts/bump-version.sh sets it
SARFEH_VERSION = "0.5.0"

#: the key Pendar's price reader signs Sarfeh's price catalog with; an installation trusts no other catalog
SARFEH_CATALOG_PUBLIC_KEY = "FgZ6B1RBtESN+VYQYBxzc4N7dlQF8D96+ExCsIe4ZAw="
#: the price service's mirrors, tried in this order: in Iran first, so a server in Iran still updates when the way out
#: is slow or closed, then outside Iran
SARFEH_PRICE_SERVICE = "https://sarfeh-api-ir.pendarsolutions.ir,https://sarfeh-api.pendarsolutions.ir"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FINOPS_", extra="ignore")

    env: Literal["dev", "test", "staging", "prod"] = "dev"
    database_url: str = f"sqlite:///{(ROOT / 'var' / 'finops-dev.db').as_posix()}"
    redis_url: str | None = None
    jwt_secret: str = "dev-only-change-me-not-for-production-use"
    jwt_ttl_minutes: int = 720
    #: Fernet key (urlsafe base64, 32 bytes) for provider credentials at rest
    credentials_key: str | None = None
    storage_dir: Path = ROOT / "var" / "uploads"
    signed_url_ttl_seconds: int = 300
    invoice_retention_days: int = 30
    max_upload_mb: int = 10
    seed_dir: Path = ROOT / "data" / "seeds"
    #: "inline" runs price syncs in the request (dev/test); "celery" enqueues to the worker
    sync_mode: Literal["inline", "celery"] = "inline"
    #: "anthropic" = Claude via the official SDK; "offline" = deterministic rule-based parser and templated
    #: advisor (no external call - useful where the LLM API is unreachable). Neither path computes prices.
    ai_provider: Literal["anthropic", "offline"] = "offline"
    ai_model: str = "claude-opus-5-5"
    anthropic_api_key: str | None = None
    ai_rate_limit_per_minute: int = 20
    #: where prices come from. "sarfeh": Sarfeh's price service, where Pendar publishes the providers' prices it reads
    #: twice a day, signed; this installation checks it every hour and says when new prices are out. "scrape": this
    #: server reads the providers' pages itself, twice a day.
    price_source: Literal["sarfeh", "scrape"] = "sarfeh"
    price_service_urls: str = SARFEH_PRICE_SERVICE
    #: apply new prices from the price service as soon as they're found, instead of waiting for an admin
    price_auto_update: bool = False
    #: a signed price catalog of your own (static files: catalog.json and catalog.json.sig), pulled twice a day in
    #: place of both of the above
    catalog_url: str | None = None
    catalog_public_key: str | None = SARFEH_CATALOG_PUBLIC_KEY
    #: publisher: scrape as usual and write a signed catalog here after each run
    catalog_publish_dir: Path | None = None
    catalog_signing_key: str | None = None
    #: optional: the collector key on each server then only works from this address
    collector_source_ip: str | None = None
    #: alerts (both optional)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    alert_email_to: str | None = None
    bale_bot_token: str | None = None
    bale_chat_id: str | None = None
    #: the shared Bale bot's relay. Nothing is sent to it until an admin links a chat in Settings.
    relay_url: str | None = None
    #: the address people open the app at, such as https://finops.example.ir. Links in emails (password reset) are
    #: built from this, never from the request, so a forged Host header can't redirect them. Without it, people ask
    #: an admin to reset their password instead.
    public_url: str | None = None
    password_reset_minutes: int = 30
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]
    #: open sign-up. The first account can always register; later users are added by an admin.
    allow_registration: bool = False
    login_rate_limit_per_minute: int = 10
    #: a public, read-only demo: visitors get a session without an account, in a shared sample workspace, and every
    #: change is refused (see demo.py). For an installation of its own, never one that holds real data.
    demo_mode: bool = False

    @property
    def is_prod(self) -> bool:
        return self.env in ("staging", "prod")

    @property
    def uses_price_service(self) -> bool:
        return self.price_source == "sarfeh" and not self.catalog_url and bool(self.price_service_list)

    @property
    def price_service_list(self) -> list[str]:
        return [u.strip().rstrip("/") for u in self.price_service_urls.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.is_prod:
        if s.jwt_secret.startswith("dev-only-change-me") or len(s.jwt_secret) < 32:
            raise RuntimeError("FINOPS_JWT_SECRET must be set in staging/prod")
        if not s.credentials_key:
            raise RuntimeError("FINOPS_CREDENTIALS_KEY must be set in staging/prod")
        if "finops-dev-password" in s.database_url:
            raise RuntimeError("POSTGRES_PASSWORD must be changed from the development default in staging/prod")
    if s.demo_mode:
        s.ai_provider = "offline"  # strangers never spend an API key
    return s
