from funghi_r6.sir import parse_rainfall_html, fetch_official_rain


def test_rainfall_parser_keeps_official_windows_separate():
    cells = ["TOS01000025","Vara","Carrara","MS","V","409","0.0","02/10/2026 06:00","1","2","5","7","10","15","30","3"]
    html = "<html><body>Dati riferiti al 02/10/2026 06:00<table><tr>" + "".join(f"<td>{x}</td>" for x in cells) + "</tr></table></body></html>"
    parsed = parse_rainfall_html(html)
    assert len(parsed["records"]) == 1
    r = parsed["records"][0]
    assert r["rain_5d_mm"] == 5
    assert r["rain_7d_mm"] == 7
    assert r["rain_15d_mm"] == 15
    assert r["rain_30d_mm"] == 30
    assert parsed["reference_time"] == "02/10/2026 06:00"


def test_rainfall_parser_tolerates_unclosed_td_like_legacy_sir():
    cells = ["TOS01000056","Pian della Fioba","Massa","MS","V","860","0.0","02/10 06.00","0","0","8.3","9.3","36.1","36.7","117.2","3"]
    # Nessun </td>: è il caso che il parser R6 originario perdeva.
    html = "<h4>Dati riferiti al 02/10/2026 06:00</h4><table><tr>" + "".join(f"<td>{x}" for x in cells) + "</tr></table>"
    parsed = parse_rainfall_html(html)
    assert len(parsed["records"]) == 1
    r = parsed["records"][0]
    assert r["code"] == "TOS01000056"
    assert r["rain_5d_mm"] == 8.3
    assert r["rain_15d_mm"] == 36.7
    assert r["rain_30d_mm"] == 117.2


def test_rainfall_parser_accepts_delimited_rendered_text():
    row = "TOS01000544 | Pisa (Fac. Agraria) | Pisa | PI | A6 | 6 | 0.0 | 02/10 06.00 | 0 | 0 | 4.1 | 5.2 | 8.0 | 10.4 | 22.7 | 2"
    parsed = parse_rainfall_html(row)
    assert len(parsed["records"]) == 1
    assert parsed["records"][0]["rain_30d_mm"] == 22.7


class ReachableOfficial:
    def get_text(self, url):
        cells = ["TOS01000025","Vara","Carrara","MS","V","409","0","02/10 06.00","1","2","5","7","10","15","30","3"]
        return "<tr>" + "".join(f"<td>{x}" for x in cells) + "</tr>"


def test_fetch_health_counts_parsed_official_records():
    data, health = fetch_official_rain(ReachableOfficial(), minimum_records=1)
    assert len(data) == 1
    assert health["reachable"] is True
    assert health["acquisition_usable"] is True
    # Senza expected_codes possiamo certificare che la sorgente è utilizzabile,
    # non che copra integralmente il catalogo strutturale.
    assert health["acquisition_complete"] is False
    assert health["expected_catalog"] is None
    assert health["fetch_mode"] == "direct"
    assert health["last_error"] is None
