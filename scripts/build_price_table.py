"""Write the public price table, site/public/prices.json, from the price catalog in data/seeds.

    python scripts/build_price_table.py            # rewrite the file
    python scripts/build_price_table.py --check    # fail if the file is out of date (the tests do the same)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))

from pricing_engine import load_seed_snapshot  # noqa: E402
from pricing_engine.size_table import size_table  # noqa: E402

OUT = ROOT / "site" / "public" / "prices.json"


def render() -> str:
    return json.dumps(size_table(load_seed_snapshot()), ensure_ascii=False, separators=(",", ":")) + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv[1:]:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT.relative_to(ROOT)} is out of date: run python scripts/build_price_table.py")
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text.encode()) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
