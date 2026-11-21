#!/usr/bin/env python3
"""Refuses to publish a catalog that looks broken, and writes docs/status.json (what the last run built).

    python3 check_feed.py docs/swu_catalog.json docs/status.json
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

catalog_path, status_path = Path(sys.argv[1]), Path(sys.argv[2])
catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
sets, cards, variants = catalog.get("sets", []), catalog.get("cards", []), catalog.get("variants", [])
problems = []
if catalog.get("schema") != 5:
    problems.append(f"schema {catalog.get('schema')} (the app reads 5)")
if len(sets) < 20 or len(cards) < 2500:
    problems.append(f"only {len(sets)} sets / {len(cards)} cards")
ids = [c["id"] for c in cards] + [v["id"] for v in variants]
if len(ids) != len(set(ids)):
    problems.append("duplicate card/variant ids")
if problems:
    sys.exit("Not publishing: " + "; ".join(problems))

art_dir = catalog_path.parent / "art"
status = {
    "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "generated": catalog.get("generated"),
    "sets": len(sets), "cards": len(cards), "variants": len(variants),
    "mirroredArt": sum(1 for f in art_dir.rglob("*.webp")) if art_dir.exists() else 0,
}
status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
print(json.dumps(status))
