from pathlib import Path

def test_morning_full_schedule_0400_sla():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "r6-builder.yml").read_text(encoding="utf-8")
    assert text.count("45 2 * * *") == 2
    assert "5 6 * * *" not in text
    assert "30 5 * * *" not in text
    assert "5 12 * * *" in text
    assert "5 18 * * *" in text
    assert 'echo "value=full"' in text
    assert 'echo "value=light"' in text
