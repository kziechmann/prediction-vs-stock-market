"""Minimal, safe Markdown -> HTML for the weekly brief.

Everything is HTML-escaped first; only a whitelisted subset is then re-introduced:
headings (#..####), paragraphs, bullet and numbered lists, **bold**, *italic*,
`code`, [links](https://...) (http/https only), horizontal rules and simple pipe tables.
Market questions quoted in the brief come from third parties, so nothing raw passes through.
"""

from __future__ import annotations

import html
import re

LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
BOLD = re.compile(r"\*\*(.+?)\*\*")
ITAL = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
CODE = re.compile(r"`([^`]+)`")


def inline(text: str) -> str:
    s = html.escape(text, quote=True)
    codes: list[str] = []

    def keep_code(m):
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    s = CODE.sub(keep_code, s)
    s = LINK.sub(lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>', s)
    s = BOLD.sub(r"<strong>\1</strong>", s)
    s = ITAL.sub(r"<em>\1</em>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], s)


def _table(rows: list[str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    if len(cells) < 2 or not all(re.fullmatch(r":?-{2,}:?", c) for c in cells[1]):
        return "".join(f"<p>{inline(r)}</p>" for r in rows)
    head = "".join(f"<th>{inline(c)}</th>" for c in cells[0])
    body = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in cells[2:])
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render(md: str) -> str:
    out: list[str] = []
    para: list[str] = []
    lst: list[str] = []
    lst_tag = None
    table: list[str] = []

    def flush():
        nonlocal lst_tag
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()
        if lst:
            out.append(f"<{lst_tag}>" + "".join(f"<li>{inline(i)}</li>" for i in lst) + f"</{lst_tag}>")
            lst.clear()
            lst_tag = None
        if table:
            out.append(_table(table))
            table.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if line.lstrip().startswith("|"):
            if para or lst:
                flush()
            table.append(line)
            continue
        elif table:
            flush()
        h = re.match(r"^(#{1,4})\s+(.*)$", line)
        if h:
            flush()
            level = min(len(h.group(1)) + 1, 5)  # page owns h1
            out.append(f"<h{level}>{inline(h.group(2))}</h{level}>")
            continue
        if re.match(r"^\s*([-*_])\s*\1\s*\1[\s\1]*$", line):
            flush()
            out.append("<hr>")
            continue
        b = re.match(r"^\s*[-*+]\s+(.*)$", line)
        n = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if b or n:
            tag = "ul" if b else "ol"
            if para or (lst_tag and lst_tag != tag):
                flush()
            lst_tag = tag
            lst.append((b or n).group(1))
            continue
        if lst and raw.startswith(("  ", "\t")):
            lst[-1] += " " + line.strip()
            continue
        if lst:
            flush()
        para.append(line.strip())
    flush()
    return "\n".join(out)
