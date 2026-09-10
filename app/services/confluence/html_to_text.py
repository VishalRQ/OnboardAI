"""Confluence storage format (XHTML + ac: macros) -> markdown-ish plain text.

Headings become markdown so the chunker can split on them; tables and code
blocks survive intact because chunking must not break them apart.
"""

import re

from bs4 import BeautifulSoup, NavigableString, Tag

#: macros that render navigation or generated indexes, never prose
DROP_MACROS = {
    "toc", "children", "pagetree", "recently-updated", "contributors",
    "livesearch", "navmap", "include", "excerpt-include", "gallery",
}

HEADINGS = {f"h{i}": "#" * i for i in range(1, 7)}

#: tags that start a new block; anything else inside a paragraph is inline
BLOCK_TAGS = {"table", "ul", "ol", "ac:structured-macro", "div", "section",
              *HEADINGS}


def _has_block_child(node: Tag) -> bool:
    return any(d.name and d.name.lower() in BLOCK_TAGS for d in node.find_all(True))


def _inline_text(node: Tag) -> str:
    """Flatten a paragraph, keeping linked page titles as words."""
    for link in node.find_all("ac:link"):
        if not link.get_text(strip=True):
            page = link.find("ri:page")
            if page and page.get("ri:content-title"):
                link.replace_with(NavigableString(page["ri:content-title"]))
    flat = " ".join(_clean(node.get_text(" ", strip=True)).split())
    return re.sub(r"\s+([.,;:!?)\]])", r"\1", flat)


#: Confluence templates sometimes store the two characters \ and n as literal
#: text rather than a line break; rendering them verbatim leaks "\n" into answers
ESCAPE_RE = re.compile(r"\\+[nrt]")


def _clean(text: str) -> str:
    """Drop literal escape sequences that are text, not markup."""
    return ESCAPE_RE.sub(" ", text)


def _table_to_markdown(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = [
            " ".join(c.get_text(" ", strip=True).split())
            for c in tr.find_all(["th", "td"])
        ]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "|" + "|".join([" --- "] * width) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _macro_to_text(macro: Tag) -> str:
    name = (macro.get("ac:name") or "").lower()
    if name in DROP_MACROS:
        return ""
    if name == "code":
        body = macro.find("ac:plain-text-body")
        code = body.get_text() if body else ""
        lang = ""
        for param in macro.find_all("ac:parameter"):
            if (param.get("ac:name") or "") == "language":
                lang = param.get_text(strip=True)
        return f"```{lang}\n{code.strip()}\n```"
    # info / note / warning / expand and friends: keep the prose inside
    rich = macro.find("ac:rich-text-body")
    return _render(rich) if rich else macro.get_text(" ", strip=True)


def _render(node) -> str:
    """Walk the tree, emitting block-level text."""
    parts: list[str] = []
    for child in getattr(node, "children", []):
        if isinstance(child, NavigableString):
            text = _clean(str(child)).strip()
            if text:
                parts.append(text)
            continue
        if not isinstance(child, Tag):
            continue

        tag = child.name.lower()
        if tag in HEADINGS:
            parts.append(f"\n{HEADINGS[tag]} {child.get_text(' ', strip=True)}\n")
        elif tag == "table":
            parts.append("\n" + _table_to_markdown(child) + "\n")
        elif tag in ("ul", "ol"):
            for i, li in enumerate(child.find_all("li", recursive=False), start=1):
                bullet = f"{i}." if tag == "ol" else "-"
                parts.append(f"{bullet} {li.get_text(' ', strip=True)}")
        elif tag == "ac:structured-macro":
            rendered = _macro_to_text(child)
            if rendered:
                parts.append("\n" + rendered + "\n")
        elif tag in ("ac:image", "ri:attachment"):
            continue  # attachments are out of scope for v1
        elif tag == "ac:link":
            page = child.find("ri:page")
            label = child.get_text(" ", strip=True)
            title = page.get("ri:content-title") if page else ""
            if label or title:
                parts.append(label or title)
        elif tag in ("p", "blockquote", "td", "th", "span", "li"):
            # a paragraph of inline markup must stay on one line
            rendered = _render(child) if _has_block_child(child) else _inline_text(child)
            if rendered:
                parts.append(rendered)
        elif tag == "br":
            parts.append("\n")
        else:
            rendered = _render(child)
            if rendered:
                parts.append(rendered)
    return "\n".join(p for p in parts if p.strip())


def storage_to_text(storage_html: str) -> str:
    """Convert one page's storage-format body to clean text."""
    if not storage_html:
        return ""
    soup = BeautifulSoup(storage_html, "html.parser")
    text = _clean(_render(soup))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
