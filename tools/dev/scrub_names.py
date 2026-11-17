"""Replaces real names (customers, people) with neutral names in comments and tests.

Usage: python3 tools/dev/scrub_names.py [--check]

The mapping is NOT kept in the repository (it would reveal the names itself): it is read from
a private JSON file, {"map": [["<regex>", "<neutral>"], ...]}, at CONNECT_SCRUB_MAP or
~/.config/revbridge-connect/scrub_map.json. The mapping is fixed: the same real name always
becomes the same neutral name, so tests that compare fixtures stay consistent.
"""

import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MAP_PATH = os.getenv("CONNECT_SCRUB_MAP") or os.path.expanduser("~/.config/revbridge-connect/scrub_map.json")
SKIP = {".git", "node_modules", ".venv", "__pycache__", "secrets", "dist", "internal-docs"}
EXT = (".py", ".sql", ".md", ".yaml", ".yml", ".toml", ".sh", ".j2", ".lua", ".conf", ".cf", ".json", ".ts", ".tsx")


def load_map() -> list[tuple[str, str]]:
    try:
        with open(MAP_PATH, encoding="utf-8") as fh:
            return [(rx, rep) for rx, rep in json.load(fh)["map"]]
    except FileNotFoundError:
        sys.exit(f"private mapping not found: {MAP_PATH} (set CONNECT_SCRUB_MAP)")


def main() -> int:
    check = "--check" in sys.argv
    mapping = load_map()
    changed = 0
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in SKIP]
        for fn in fns:
            if not fn.endswith(EXT) or fn == "scrub_names.py":
                continue
            p = os.path.join(dp, fn)
            try:
                txt = open(p, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            new = txt
            for rx, rep in mapping:
                new = re.sub(rx, rep, new)
            if new != txt:
                changed += 1
                rel = os.path.relpath(p, ROOT)
                if check:
                    print("has a real name:", rel)
                else:
                    open(p, "w", encoding="utf-8").write(new)
                    print("cleaned:", rel)
    print(f"{changed} file(s) {'with real names' if check else 'changed'}")
    return 1 if (check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
