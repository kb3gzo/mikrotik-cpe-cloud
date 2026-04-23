"""Application settings — loaded from environment / .env via pydantic-settings."""
from __future__ import annotations

from functools import lru_cache
from ipaddress import IPv4Network
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Values are read from environment variables, falling back to a local `.env`
    file if one exists. Keys are case-insensitive on the env side.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql+psycopg://cpecloud:cpecloud@127.0.0.1:5432/cpecloud",
        description="SQLAlchemy async URL. Use postgresql+psycopg:// for psycopg3 async.",
    )

    # InfluxDB
    influx_url: str = "http://127.0.0.1:8086"
    influx_org: str = "bradford"
    influx_bucket: str = "cpe-system-raw"
    influx_token: str = ""

    # Public identity
    server_fqdn: str = "mcc.bradfordbroadband.com"
    enrollment_url: str = "https://mcc.bradfordbroadband.com/api/v1/auto-enroll"

    # WireGuard
    wg_interface: str = "wg0"
    wg_overlay_cidr: str = "10.100.0.0/22"
    wg_server_ip: str = "10.100.0.1"
    wg_server_public_key: str = ""
    wg_server_endpoint: str = "mcc.bradfordbroadband.com:51820"
    wg_config_path: Path = Path("/etc/wireguard/wg0.conf")
    wg_sync_helper: Path = Path("/usr/local/sbin/cpe-cloud-wg-sync")

    # Provisioning secrets (plaintext — see docs/01-design §secrets).
    # `current` is embedded in newly-issued factory installers. `previous`
    # continues to validate enrollments from shelf stock pre-provisioned
    # before the last rotation (grace window per 02-self-provisioning.md §2.2).
    provisioning_secret_current: str = ""
    provisioning_secret_previous: str = ""

    # App
    app_log_level: str = "INFO"
    app_env: str = "dev"

    @field_validator("wg_overlay_cidr")
    @classmethod
    def _validate_cidr(cls, v: str) -> str:
        # Raises if malformed — fail fast at startup rather than at enroll time.
        IPv4Network(v)
        return v

    @property
    def overlay_network(self) -> IPv4Network:
        return IPv4Network(self.wg_overlay_cidr)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton. Tests can override via `dependency_overrides`."""
    return Settings()
