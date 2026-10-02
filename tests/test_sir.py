from funghi_r6.sir import parse_rainfall_html


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
