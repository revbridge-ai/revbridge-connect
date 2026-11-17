#!/usr/bin/env python3
"""Dump FastAPI OpenAPI specs to disk for git-tracking.

Connect has 3 FastAPI apps (API, tracking and events-updater). Each one gets its own JSON
file in docs/openapi/.

Usage:
    uv sync --frozen --all-packages
    uv run python tools/dump_openapi.py

Always re-sync first — output is sensitive to fastapi/pydantic
versions and CI runs `uv sync --frozen --all-packages`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("OTEL_SDK_DISABLED", "true")

ROOT = Path(__file__).resolve().parent.parent

APPS: list[tuple[str, str]] = [
    ("mail-api.json", "mail_api.app.api.app:app"),
    ("mail-tracking.json", "mail_tracking.app.tracking.app:app"),
    ("mail-events-updater.json", "mail_milter.events_updater.app:app"),
]

OUT_DIR = ROOT / "docs" / "openapi"


def _load_app(spec: str):
    module_path, attr = spec.split(":")
    mod = __import__(module_path, fromlist=[attr])
    return getattr(mod, attr)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for filename, app_spec in APPS:
        try:
            app = _load_app(app_spec)
            spec = app.openapi()
        except Exception as e:
            print(f"FAIL {app_spec}: {e}", file=sys.stderr)
            failures += 1
            continue
        out_path = OUT_DIR / filename
        out_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
        print(f"wrote {out_path.relative_to(ROOT)} ({len(spec.get('paths', {}))} paths)")
    return failures


if __name__ == "__main__":
    sys.exit(main())
