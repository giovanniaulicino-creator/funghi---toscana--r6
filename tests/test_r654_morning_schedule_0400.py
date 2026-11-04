from pathlib import Path

import yaml


def test_daily_workflow_serializes_writers_and_has_morning_fallbacks():
    root = Path(__file__).resolve().parents[1]
    # BaseLoader keeps the GitHub YAML key "on" as text (not YAML 1.1 boolean).
    workflow = yaml.load((root / ".github/workflows/r6-builder.yml").read_text(), Loader=yaml.BaseLoader)
    events = workflow["on"]
    assert events["schedule"] == [{"cron": "17 1,3,6,9 * * *", "timezone": "Europe/Rome"}]
    assert events["workflow_dispatch"]["inputs"]["mode"]["default"] == "full"
    assert events["workflow_dispatch"]["inputs"]["force_rebuild"]["default"] == "false"
    assert workflow["concurrency"] == {"group": "funghi-toscana-r6-builder", "cancel-in-progress": "false"}
    steps = workflow["jobs"]["build"]["steps"]
    build = next(s for s in steps if s.get("name") == "Build or reuse today's scientific snapshot")
    assert "--daily" in build["run"]
    assert "${{" not in build["run"]  # Inputs enter as quoted environment values, never shell source.
    assert "--target-day" in build["run"]
    assert "--force-rebuild" in build["run"]
