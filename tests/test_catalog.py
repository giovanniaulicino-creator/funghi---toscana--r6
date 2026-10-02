from funghi_r6.catalog import normalize_wfs


def test_normalize_wfs_detects_station_code_and_coordinates():
    payload={"features":[{"properties":{"codice":"TOS01000025","nome":"Vara","comune":"Carrara","provincia":"MS","quota":409},"geometry":{"coordinates":[10.12965203,44.08376263]}}]}
    rows=normalize_wfs(payload)
    assert rows[0]["code"] == "TOS01000025"
    assert rows[0]["lat"] == 44.08376263
