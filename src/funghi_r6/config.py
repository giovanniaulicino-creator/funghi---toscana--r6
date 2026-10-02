from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    expected_stations: int = int(os.getenv("R6_EXPECTED_STATIONS", "418"))
    batch_size: int = int(os.getenv("R6_BATCH_SIZE", "12"))
    batch_pause_s: float = float(os.getenv("R6_BATCH_PAUSE_S", "1.5"))
    max_retries: int = int(os.getenv("R6_MAX_RETRIES", "5"))
    timezone: str = "Europe/Rome"
    model_version: str = "4.9.0-data-completeness-r6"
    cf_account_id: str | None = os.getenv("CF_ACCOUNT_ID") or None
    cf_d1_database_id: str | None = os.getenv("CF_D1_DATABASE_ID") or None
    cf_api_token: str | None = os.getenv("CF_API_TOKEN") or None

    @property
    def d1_enabled(self) -> bool:
        return bool(self.cf_account_id and self.cf_d1_database_id and self.cf_api_token)


SETTINGS = Settings()
