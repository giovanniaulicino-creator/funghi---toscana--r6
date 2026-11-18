"""Verify the installed IANA database on hosts without system time-zone files."""
import json
import os
import subprocess
import sys


def test_rome_timezone_works_without_system_database():
    program = """
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
rome = ZoneInfo('Europe/Rome')
instants = ['2026-11-18T05:30:00+00:00', '2026-07-18T05:30:00+00:00',
            '2026-11-18T23:30:00+00:00']
print(json.dumps([datetime.fromisoformat(value).astimezone(rome).isoformat() for value in instants]))
"""
    result = subprocess.run([sys.executable, "-c", program],
        env={**os.environ, "PYTHONTZPATH": ""}, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "2026-11-18T06:30:00+01:00", "2026-07-18T07:30:00+02:00",
        "2026-11-19T00:30:00+01:00",
    ]
