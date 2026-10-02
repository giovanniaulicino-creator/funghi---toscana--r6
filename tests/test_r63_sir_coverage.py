from funghi_r6.sir import fetch_official_rain


def _row(code, r5="5", r7="7", r15="15", r30="30"):
    cells = [
        code, "Stazione", "Comune", "PI", "A", "100",
        "0", "02/10 02.00", "1", "2", r5, r7, "10", r15, r30, "3",
    ]
    return "<tr>" + "".join(f"<td>{x}</td>" for x in cells) + "</tr>"


class OneStationSir:
    def get_text(self, url):
        return (
            "<html><body>Dati riferiti al 02/10/2026 02:00<table>"
            + _row("TOS00000001")
            + "</table></body></html>"
        )


def test_usable_is_not_full_catalog_coverage():
    data, health = fetch_official_rain(
        OneStationSir(),
        minimum_records=1,
        allow_browser=False,
        expected_codes={"TOS00000001", "TOS00000002"},
    )

    assert len(data) == 1
    assert health["acquisition_usable"] is True
    assert health["acquisition_complete"] is False
    assert health["expected_catalog"] == 2
    assert health["matched_catalog"] == 1
    assert health["missing_catalog"] == 1
    assert health["missing_codes"] == ["TOS00000002"]
    assert health["complete_windows_5_7_15_30"] == 1
    assert health["fallback_needed"] == 1
    assert health["fallback_needed_codes"] == ["TOS00000002"]


class TwoStationsOneIncomplete:
    def get_text(self, url):
        return (
            "<html><body>Dati riferiti al 02/10/2026 02:00<table>"
            + _row("TOS00000001")
            + _row("TOS00000002", r5="-")
            + "</table></body></html>"
        )


def test_present_but_incomplete_station_requires_fallback():
    _, health = fetch_official_rain(
        TwoStationsOneIncomplete(),
        minimum_records=1,
        allow_browser=False,
        expected_codes={"TOS00000001", "TOS00000002"},
    )

    assert health["matched_catalog"] == 2
    assert health["missing_catalog"] == 0
    assert health["acquisition_complete"] is False
    assert health["complete_windows_5_7_15_30"] == 1
    assert health["incomplete_window_codes"] == ["TOS00000002"]
    assert health["field_coverage"]["rain_5d_mm"] == 1
    assert health["field_coverage"]["rain_30d_mm"] == 2
    assert health["fallback_needed"] == 1


class TwoCompleteStations:
    def get_text(self, url):
        return (
            "<html><body>Dati riferiti al 02/10/2026 02:00<table>"
            + _row("TOS00000001")
            + _row("TOS00000002")
            + "</table></body></html>"
        )


def test_full_catalog_complete_only_when_all_expected_have_all_windows():
    _, health = fetch_official_rain(
        TwoCompleteStations(),
        minimum_records=1,
        allow_browser=False,
        expected_codes={"TOS00000001", "TOS00000002"},
    )

    assert health["acquisition_usable"] is True
    assert health["acquisition_complete"] is True
    assert health["missing_catalog"] == 0
    assert health["complete_windows_5_7_15_30"] == 2
    assert health["fallback_needed"] == 0
