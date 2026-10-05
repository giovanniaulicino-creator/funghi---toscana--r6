from pathlib import Path


def test_r641_d1_change_only_writes_are_present():
    root=Path(__file__).resolve().parents[1]
    text=(root/"src/funghi_r6/d1.py").read_text(encoding="utf-8")
    assert "R641-D1-WRITE-BUDGET-2026-10-05" in text
    assert "WHERE name IS NOT excluded.name" in text
    assert "WHERE payload_json IS NOT excluded.payload_json" in text
    assert "WHERE rain_mm IS NOT excluded.rain_mm" in text
    assert "WHERE reachable IS NOT excluded.reachable" in text


def test_r641_workflow_does_not_auto_bootstrap_on_push():
    root=Path(__file__).resolve().parents[1]
    text=(root/".github/workflows/r6-builder.yml").read_text(encoding="utf-8")
    assert "R641-D1-BUDGET" in text
    assert "\n  push:" not in text
    assert 'github.event_name == "push"' not in text
    assert "workflow_dispatch:" in text
    assert "schedule:" in text
