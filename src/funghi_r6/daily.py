"""Rome-day orchestration only; no scientific calculations live here."""
from __future__ import annotations

from datetime import date, datetime
import re
from zoneinfo import ZoneInfo


DAILY_COVERAGE = (
    "structural", "current", "rain_5_7_15_30", "history_30", "history_90",
    "long_hydro_90", "forecast_7", "forecast_15", "et0", "soil", "scientific_complete",
)


def rome_day() -> str:
    return datetime.now(ZoneInfo("Europe/Rome")).date().isoformat()


def validate_day(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("target-day deve essere YYYY-MM-DD")
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("target-day non valido")
    return value


def complete_daily_manifest(manifest: dict, target_day: str, expected: int) -> bool:
    """Accept only a commit certificate matching the requested scientific day."""
    try:
        proof = manifest.get("d1_verification") or {}
        return (
            manifest.get("architecture") == "R6"
            and manifest.get("publication") == "ACTIVE"
            and manifest.get("target_day") == target_day
            and manifest.get("scientifically_complete") is True
            and manifest.get("station_count") == expected
            and all((manifest.get("coverage") or {}).get(k) == expected for k in DAILY_COVERAGE)
            and isinstance(manifest.get("generation"), str)
            and bool(manifest["generation"])
            and bool(re.fullmatch(r"[a-f0-9]{64}", manifest.get("sha256") or ""))
            and isinstance(manifest.get("snapshot_bytes"), int)
            and manifest["snapshot_bytes"] > 0
            and proof.get("readback_verified") is True
            and proof.get("active_generation") == manifest["generation"]
            and proof.get("sha256") == manifest["sha256"]
            and proof.get("snapshot_bytes") == manifest["snapshot_bytes"]
            and proof.get("station_rows") == expected
            and isinstance(proof.get("snapshot_chunks"), int)
            and proof["snapshot_chunks"] > 0
        )
    except (AttributeError, TypeError, ValueError):
        return False
