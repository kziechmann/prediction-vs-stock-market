#!/usr/bin/env python3
"""Build the dashboard from data/latest.json, analysis/notes.json and the newest brief.

  python3 info_arb/render.py

Writes:
  site/index.html      full HTML document (GitHub Pages / any static host / open locally)
  site/artifact.html   the same page without the <html>/<head>/<body> shell, for publishing
                       as a claude.ai Artifact (which supplies its own shell)
All data is embedded, so each file is self-contained.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from infoarb import mdlite

ROOT = Path(__file__).resolve().parent
SHELL_HEAD = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
              '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n')


def latest_brief(folder: Path) -> tuple[str | None, str | None]:
    briefs = sorted(p for p in folder.glob("*.md") if re.match(r"\d{4}-\d{2}-\d{2}", p.stem))
    if not briefs:
        return None, None
    p = briefs[-1]
    return p.stem[:10], p.read_text()


def build(data_path: Path, notes_path: Path, briefs: Path, template: Path) -> tuple[str, str]:
    data = json.loads(data_path.read_text()) if data_path.exists() else None
    notes = json.loads(notes_path.read_text()) if notes_path.exists() else {}
    brief_date, brief_md = latest_brief(briefs)
    payload = {
        "data": data,
        "brief": {"date": brief_date, "html": mdlite.render(brief_md)} if brief_md else None,
        "notes_updated": notes.get("updated"),
    }
    # JSON inside <script> must not be able to close the tag.
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/").replace("<!--", "<\\!--")
    tpl = template.read_text()
    head, body = tpl.split("<!--BODY-->", 1)
    head = head.replace("<!--HEAD-->", "")
    body = body.replace("__PAYLOAD__", blob)
    fragment = head + body
    full = SHELL_HEAD + head + "</head>\n<body>\n" + body + "\n</body>\n</html>\n"
    return full, fragment


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=ROOT / "data" / "latest.json")
    ap.add_argument("--notes", type=Path, default=ROOT / "analysis" / "notes.json")
    ap.add_argument("--briefs", type=Path, default=ROOT / "analysis" / "briefs")
    ap.add_argument("--out", type=Path, default=ROOT / "site")
    args = ap.parse_args(argv)
    full, fragment = build(args.data, args.notes, args.briefs, ROOT / "templates" / "dashboard.html")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(full)
    (args.out / "artifact.html").write_text(fragment)
    print(f"wrote {args.out / 'index.html'} ({len(full) // 1024} KB) and artifact.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
