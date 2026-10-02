from __future__ import annotations

from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import os
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

RAINFALL_SOURCES = [
    "https://www.sir.toscana.it/monitoraggio/stazioni.php?type=pluvio_men",
    "https://sir.toscana.it/monitoraggio/stazioni.php?type=pluvio_men",
    "https://www.cfr.toscana.it/monitoraggio/stazioni.php?type=pluvio_men",
    "https://demo.sir.toscana.it/monitoraggio/stazioni.php?type=pluvio_men",
]

_STATION_RE = re.compile(r"^TOS\d{8,}$", re.I)
_STATION_ANY_RE = re.compile(r"TOS\d{8,}", re.I)


def _is_station_code(value: Any) -> bool:
    return bool(_STATION_RE.fullmatch(str(value or "").strip()))


def _text_from_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", value or "", flags=re.I)
    text = re.sub(r"<script\b[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style\b[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(unescape(text).replace("\xa0", " ").split())


class _TableParser(HTMLParser):
    """Parser normale, ma tollera anche una nuova cella prima della chiusura della precedente."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def _close_cell(self) -> None:
        if self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
        self._cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self._row:
            self.rows.append(self._row)
        self._row = None

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag == "tr":
            # HTML SIR storico può non chiudere perfettamente la riga precedente.
            if self._row is not None:
                self._close_row()
            self._row = []
        elif tag in {"td", "th"}:
            if self._row is None:
                return
            # Importante: alcuni HTML SIR aprono il td successivo senza </td>.
            if self._cell is not None:
                self._close_cell()
            self._cell = []

    def handle_data(self, data: str):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in {"td", "th"}:
            self._close_cell()
        elif tag == "tr":
            self._close_row()

    def close(self):
        super().close()
        if self._row is not None:
            self._close_row()


def _parse_html_rows(html: str) -> list[list[str]]:
    parser = _TableParser()
    parser.feed(html or "")
    parser.close()
    return parser.rows


def _parse_loose_html_rows(html: str) -> list[list[str]]:
    """Fallback portato dal parser R5 collaudato: usa gli start-tag delle celle.

    Non dipende dalla presenza di </td>; è quindi adatto alle pagine legacy SIR.
    """
    rows: list[list[str]] = []
    seen_starts: set[int] = set()
    source = html or ""
    for code_match in _STATION_ANY_RE.finditer(source):
        start = source.rfind("<tr", 0, code_match.start() + 1)
        closing = source.find("</tr", code_match.end())
        if start < 0 or closing < 0 or start in seen_starts:
            continue
        seen_starts.add(start)
        row_html = source[start:closing]
        openings = list(re.finditer(r"<(?:td|th)\b[^>]*>", row_html, flags=re.I))
        cells: list[str] = []
        for index, opening in enumerate(openings):
            content_start = opening.end()
            content_end = openings[index + 1].start() if index + 1 < len(openings) else len(row_html)
            cells.append(_text_from_html(row_html[content_start:content_end]))
        if cells:
            rows.append(cells)
    return rows


def _parse_delimited_rows(text: str) -> list[list[str]]:
    """Fallback per output testuale/renderizzato (pipe, tab o colonne distanziate)."""
    rows: list[list[str]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not _STATION_ANY_RE.search(line):
            continue
        if "|" in line:
            cells = line.split("|")
        elif "\t" in line:
            cells = line.split("\t")
        else:
            cells = re.split(r"\s{2,}", line)
        cells = [_text_from_html(cell).strip() for cell in cells]
        cells = [cell for cell in cells if cell != ""]
        code_index = next((i for i, cell in enumerate(cells) if _is_station_code(cell)), -1)
        if code_index >= 0:
            rows.append(cells[code_index:])
    return rows


def station_rows(html: str) -> list[list[str]]:
    candidates = [
        _parse_html_rows(html),
        _parse_loose_html_rows(html),
        _parse_delimited_rows(_text_from_html(html)),
        _parse_delimited_rows(html),
    ]
    best: list[list[str]] = []
    for rows in candidates:
        normalized: list[list[str]] = []
        for cells in rows:
            code_index = next((i for i, cell in enumerate(cells) if _is_station_code(cell)), -1)
            if code_index >= 0:
                cells = cells[code_index:]
            if cells and _is_station_code(cells[0]):
                normalized.append(cells)
        if len(normalized) > len(best):
            best = normalized
    return best


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
    plain = _text_from_html(html or "")
    m = re.search(
        r"(?:riferit[ei]|dati riferiti)\s+al\s+(\d{2}/\d{2}/\d{4}\s+\d{1,2}[.:]\d{2})",
        plain,
        flags=re.I,
    )
    return m.group(1).replace(".", ":") if m else None


def parse_rainfall_html(html: str) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for cells in station_rows(html):
        if len(cells) < 15:
            continue
        code = cells[0].strip().upper()
        if not _is_station_code(code):
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
            "rain_values_are_independent": False,
            "rain_values_are_nested": True,
        })
    return {"reference_time": parse_reference_time(html), "records": records}


def _cache_bust(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["_r6"] = str(int(time.time()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _html_diagnostics(html: str) -> dict[str, Any]:
    source = str(html or "")
    title_match = re.search(r"<title[^>]*>([\s\S]*?)</title>", source, flags=re.I)
    return {
        "bytes": len(source.encode("utf-8", errors="replace")),
        "station_code_occurrences": len(_STATION_ANY_RE.findall(source)),
        "title": _text_from_html(title_match.group(1))[:120] if title_match else None,
    }


def _render_urls_with_selenium(urls: list[str], minimum_records: int) -> list[tuple[str, str, dict[str, Any]]]:
    """Render browser reale solo se l'HTML diretto non contiene la tabella.

    GitHub-hosted Ubuntu dispone normalmente di Chrome. Selenium Manager individua
    automaticamente il browser/driver disponibili. Un fallimento del browser non
    blocca il builder: Open-Meteo resta fallback scientifico dichiarato.
    """
    rendered: list[tuple[str, str, dict[str, Any]]] = []
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.support.ui import WebDriverWait
    except Exception as exc:
        return [("", "", {"browser_error": f"selenium import: {exc}"})]

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,1600")
    options.add_argument("--lang=it-IT")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/154.0.0.0 Safari/537.36"
    )

    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(35)
        for url in urls:
            target = _cache_bust(url)
            try:
                driver.get(target)
                try:
                    WebDriverWait(driver, 18).until(
                        lambda d: len(_STATION_ANY_RE.findall(d.page_source or "")) >= minimum_records
                    )
                except Exception:
                    # Conserviamo comunque il page_source: può contenere righe utili
                    # anche se non raggiunge la soglia minima.
                    pass
                html = driver.page_source or ""
                parsed = parse_rainfall_html(html)
                rendered.append((url, html, {
                    "browser_error": None,
                    "parsed_records": len(parsed["records"]),
                    **_html_diagnostics(html),
                }))
                if len(parsed["records"]) >= minimum_records:
                    break
            except Exception as exc:
                rendered.append((url, "", {"browser_error": str(exc)}))
    except Exception as exc:
        rendered.append(("", "", {"browser_error": f"webdriver: {exc}"}))
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
    return rendered


def fetch_official_rain(
    http,
    *,
    minimum_records: int = 100,
    allow_browser: bool = False,
    expected_codes: set[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    best: dict[str, Any] = {"reference_time": None, "records": []}
    best_url: str | None = None
    best_mode: str | None = None
    reachable_url: str | None = None
    errors: list[str] = []
    candidates: list[dict[str, Any]] = []

    for url in RAINFALL_SOURCES:
        try:
            # Cache-busting evita di riutilizzare una pagina SIR vecchia/intermedia.
            try:
                html = http.get_text(_cache_bust(url))
            except TypeError:
                # Compatibilità con i fake HTTP dei test.
                html = http.get_text(url)
            if reachable_url is None:
                reachable_url = url
            parsed = parse_rainfall_html(html)
            diag = {
                "url": url,
                "mode": "direct",
                "reachable": True,
                "parsed_records": len(parsed["records"]),
                "reference_time": parsed.get("reference_time"),
                **_html_diagnostics(html),
            }
            candidates.append(diag)
            if len(parsed["records"]) > len(best["records"]):
                best, best_url, best_mode = parsed, url, "direct"
            if len(parsed["records"]) >= minimum_records:
                break
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            candidates.append({
                "url": url,
                "mode": "direct",
                "reachable": False,
                "error": str(exc),
            })

    # La R5 aveva già dimostrato che alcune risposte SIR restituiscono solo la
    # shell HTML al fetch diretto. In R6 il browser fallback vive nel Data Builder,
    # non nel Worker Cloudflare.
    if len(best["records"]) < minimum_records and allow_browser:
        for url, html, diag in _render_urls_with_selenium(RAINFALL_SOURCES, minimum_records):
            if not url:
                candidates.append({"url": None, "mode": "browser", **diag})
                continue
            parsed = parse_rainfall_html(html)
            candidates.append({
                "url": url,
                "mode": "browser",
                "reachable": True,
                "reference_time": parsed.get("reference_time"),
                **diag,
            })
            if len(parsed["records"]) > len(best["records"]):
                best, best_url, best_mode = parsed, url, "browser"
            if len(parsed["records"]) >= minimum_records:
                break

    by_code = {r["code"]: r for r in best["records"]}
    reachable = reachable_url is not None

    # Una sorgente può essere "utilizzabile" anche se non copre tutte le 418
    # stazioni strutturali. Non chiamiamo più "complete" una semplice soglia 100.
    acquisition_usable = len(by_code) >= minimum_records

    expected_set = {
        str(code or "").strip().upper()
        for code in (expected_codes or set())
        if _is_station_code(code)
    }
    acquired_set = set(by_code)

    matched_codes = sorted(expected_set & acquired_set)
    missing_codes = sorted(expected_set - acquired_set)
    extra_codes = sorted(acquired_set - expected_set) if expected_set else []

    rain_fields = ["rain_5d_mm", "rain_7d_mm", "rain_15d_mm", "rain_30d_mm"]
    field_coverage = {
        key: sum(1 for row in by_code.values() if row.get(key) is not None)
        for key in rain_fields
    }

    incomplete_window_codes = sorted(
        code for code, row in by_code.items()
        if not all(row.get(key) is not None for key in rain_fields)
    )
    complete_windows = len(by_code) - len(incomplete_window_codes)

    # Completa rispetto al catalogo significa: tutte le stazioni attese presenti
    # e tutte con le quattro finestre pluviometriche necessarie.
    acquisition_complete = bool(expected_set) and not missing_codes and not incomplete_window_codes

    needs_fallback_codes = sorted(set(missing_codes) | set(incomplete_window_codes))

    if acquisition_usable:
        last_error = None
    elif errors:
        last_error = errors[-1]
    else:
        direct_codes = max(
            (int(c.get("station_code_occurrences") or 0) for c in candidates if c.get("mode") == "direct"),
            default=0,
        )
        if direct_codes:
            last_error = (
                f"SIR/CFR raggiungibile: HTML contiene {direct_codes} codici stazione, "
                f"ma il parser ha acquisito solo {len(by_code)} record utili"
            )
        else:
            last_error = (
                "SIR/CFR raggiungibile ma la risposta diretta non contiene la tabella pluviometrica; "
                "browser fallback non disponibile o non riuscito"
            )

    now = datetime.now(timezone.utc).isoformat()
    health = {
        "source": "SIR/CFR rainfall",
        "reachable": reachable,
        "acquired": len(by_code),
        "acquisition_usable": acquisition_usable,
        "acquisition_complete": acquisition_complete,
        "expected_catalog": len(expected_set) if expected_set else None,
        "matched_catalog": len(matched_codes) if expected_set else None,
        "missing_catalog": len(missing_codes) if expected_set else None,
        "missing_codes": missing_codes,
        "extra_codes": extra_codes,
        "field_coverage": field_coverage,
        "complete_windows_5_7_15_30": complete_windows,
        "incomplete_window_codes": incomplete_window_codes,
        "fallback_needed": len(needs_fallback_codes) if expected_set else None,
        "fallback_needed_codes": needs_fallback_codes,
        "reference_time": best.get("reference_time"),
        "source_url": best_url or reachable_url,
        "fetch_mode": best_mode,
        "last_success_at": now if acquisition_usable else None,
        "last_contact_at": now if reachable else None,
        "last_error": last_error,
        "reachability_errors": errors,
        "candidates": candidates,
    }
    return by_code, health
