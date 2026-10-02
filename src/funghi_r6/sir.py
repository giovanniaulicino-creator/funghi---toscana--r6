from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from typing import Any

RAINFALL_SOURCES = [
    "https://www.sir.toscana.it/monitoraggio/stazioni.php?type=pluvio_men",
    "https://sir.toscana.it/monitoraggio/stazioni.php?type=pluvio_men",
    "https://www.cfr.toscana.it/monitoraggio/stazioni.php?type=pluvio_men",
    "https://demo.sir.toscana.it/monitoraggio/stazioni.php?type=pluvio_men",
]


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def table_rows(html: str) -> list[list[str]]:
    parser = _TableParser()
    parser.feed(html or "")
    return parser.rows


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip().replace("−", "-").replace(",", ".")
    if not text or text in {"-", "--"} or text.lower() in {"n.d.", "nd", "nan"}:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_reference_time(html: str) -> str | None:
    plain = re.sub(r"<[^>]+>", " ", html or "")
    plain = re.sub(r"\s+", " ", plain)
    m = re.search(r"(?:riferit[ei]|dati riferiti)\s+al\s+(\d{2}/\d{2}/\d{4}\s+\d{1,2}[.:]\d{2})", plain, flags=re.I)
    return m.group(1).replace(".", ":") if m else None


def parse_rainfall_html(html: str) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for cells in table_rows(html):
        if len(cells) < 15:
            continue
        code = cells[0].strip().upper()
        if not re.fullmatch(r"TOS\d{8,}", code):
            continue
        records.append({
            "code": code,
            "name": cells[1] or None,
            "municipality": cells[2] or None,
            "province": cells[3] or None,
            "zone": cells[4] or None,
            "altitude_m": _num(cells[5]),
            "rain_current_mm": _num(cells[6]),
            "rain_observed_label": cells[7] or None,
            "rain_1d_mm": _num(cells[8]),
            "rain_2d_mm": _num(cells[9]),
            "rain_5d_mm": _num(cells[10]),
            "rain_7d_mm": _num(cells[11]),
            "rain_10d_mm": _num(cells[12]),
            "rain_15d_mm": _num(cells[13]),
            "rain_30d_mm": _num(cells[14]),
            "dry_days": _num(cells[15]) if len(cells) > 15 else None,
        })
    return {"reference_time": parse_reference_time(html), "records": records}


def fetch_official_rain(http, *, minimum_records: int = 100) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    best: dict[str, Any] = {"reference_time": None, "records": []}
    best_url: str | None = None
    reachable_url: str | None = None
    errors: list[str] = []
    parse_notes: list[str] = []
    for url in RAINFALL_SOURCES:
        try:
            html = http.get_text(url)
            if reachable_url is None:
                reachable_url = url
            parsed = parse_rainfall_html(html)
            count = len(parsed["records"])
            if count > len(best["records"]):
                best, best_url = parsed, url
            if count >= minimum_records:
                break
            parse_notes.append(f"{url}: servizio raggiungibile ma tabella utile {count}/{minimum_records}")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    by_code = {r["code"]: r for r in best["records"]}
    reachable = reachable_url is not None
    acquisition_complete = len(by_code) >= minimum_records
    if acquisition_complete:
        last_error = None
    elif parse_notes:
        last_error = parse_notes[-1]
    elif errors:
        last_error = errors[-1]
    else:
        last_error = "Nessun endpoint SIR/CFR ha restituito una tabella pluviometrica utilizzabile"
    health = {
        "source": "SIR/CFR rainfall",
        "reachable": reachable,
        "acquired": len(by_code),
        "acquisition_complete": acquisition_complete,
        "reference_time": best.get("reference_time"),
        "source_url": best_url or reachable_url,
        "last_success_at": datetime.now(timezone.utc).isoformat() if reachable else None,
        "last_error": last_error,
        "reachability_errors": errors,
    }
    return by_code, health
