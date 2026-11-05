"""R6587: explain rejected inputs; never shorten or fill a scientific forecast."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math

FIELDS = ("precipitation_sum", "temperature_2m_mean", "et0_fao_evapotranspiration")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def forecast_issues(payload: object, target_day: str) -> list[dict]:
    """Same D+1..D+15 mandatory inputs as the unchanged production validator."""
    if not isinstance(payload, dict) or not isinstance(payload.get("daily"), dict):
        return [{"reason": "invalid_daily_object"}]
    daily = payload["daily"]
    dates = daily.get("time")
    if not isinstance(dates, list) or any(not isinstance(day, str) for day in dates):
        return [{"reason": "invalid_dates_array"}]
    try:
        if any(date.fromisoformat(day).isoformat() != day for day in dates):
            return [{"reason": "invalid_calendar_date"}]
    except ValueError:
        return [{"reason": "invalid_calendar_date"}]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        return [{"reason": "unordered_or_duplicate_dates"}]
    bad_fields = [field for field in FIELDS if not isinstance(daily.get(field), list)]
    if bad_fields:
        return [{"reason": "missing_or_invalid_series", "field": field} for field in bad_fields]
    issues = []
    for offset in range(1, 16):
        day = (date.fromisoformat(target_day) + timedelta(days=offset)).isoformat()
        count = dates.count(day)
        if count != 1:
            issues.append({"day": day, "offset": offset,
                           "reason": "missing_date" if count == 0 else "duplicate_date",
                           "occurrences": count})
            continue
        index = dates.index(day)
        for field in FIELDS:
            values = daily[field]
            value = values[index] if index < len(values) else None
            reason = None
            if index >= len(values):
                reason = "missing_value"
            elif value is None:
                reason = "null_value"
            else:
                try:
                    if not math.isfinite(float(value)):
                        reason = "non_finite_value"
                except (TypeError, ValueError, OverflowError):
                    reason = "non_numeric_value"
            if reason:
                issues.append({"day": day, "offset": offset, "field": field,
                               "reason": reason, "value": json_safe(value)})
    last_day = (date.fromisoformat(target_day) + timedelta(days=15)).isoformat()
    if any(item.get("offset") == 15 for item in issues) and any(day > last_day for day in dates):
        # D+16 already present means D+15 is an internal gap, not a short tail.
        issues.append({"reason": "gap_before_later_available_date"})
    return issues


def json_safe(value):
    """Preserve provider evidence, including non-finite values, in strict JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def forecast_evidence(code: str, record: dict, target_day: str) -> dict:
    payload = record.get("payload")
    issues = forecast_issues(payload, target_day)
    return {"code": code, "target_day": target_day, "fetched_at": record.get("fetched_at"),
            "timezone": payload.get("timezone") if isinstance(payload, dict) else None,
            "issues": issues,
            # Raw daily input BEFORE filtering: the next successful run must not
            # erase the evidence for a rejected date or optional coverage field.
            "daily": json_safe(payload.get("daily")) if isinstance(payload, dict) else None}


def tail_not_ready(issues: list[dict]) -> bool:
    return bool(issues) and all(
        item.get("offset") == 15
        and item.get("reason") in {"missing_date", "missing_value", "null_value"}
        for item in issues
    )


def may_wait(target_day: str, now: datetime, *, forced: bool) -> bool:
    # Global 00UTC model runs normally become available about 04..06UTC.
    # A persistent gap at/after 06UTC remains an actual failed job/notification.
    # This is an alert threshold, NOT a claim that data is ready at a fixed time.
    deadline = datetime.combine(date.fromisoformat(target_day), datetime.min.time(), timezone.utc)
    deadline += timedelta(hours=6)
    return not forced and now.astimezone(timezone.utc) < deadline


def deadline_utc(target_day: str) -> str:
    return target_day + "T06:00:00+00:00"
