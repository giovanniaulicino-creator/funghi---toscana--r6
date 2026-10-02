from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import SETTINGS
from .http import HttpClient
from .sir import fetch_official_rain


def main() -> int:
    p = argparse.ArgumentParser(description="Audit mirato SIR/CFR rainfall per Funghi Toscana R6")
    p.add_argument("--out", default="out/sir-audit.json")
    p.add_argument("--minimum", type=int, default=100)
    args = p.parse_args()

    http = HttpClient(max_retries=SETTINGS.max_retries)
    data, health = fetch_official_rain(http, minimum_records=args.minimum, allow_browser=True)
    payload = {
        "ok": len(data) >= args.minimum,
        "acquired": len(data),
        "minimum": args.minimum,
        "health": health,
        "sample": list(data.values())[:5],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["ok"]:
        raise SystemExit(f"SIR audit incompleto: {len(data)}/{args.minimum} record minimi")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
