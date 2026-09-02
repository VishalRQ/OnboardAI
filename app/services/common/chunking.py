"""Heading-aware chunking, shared by every knowledge source.

Split on heading structure first,
then recursively split any section still over budget. Every chunk carries a
breadcrumb + heading path so an isolated chunk keeps its location.

Token counts are approximated as chars/4 to avoid a tokeniser dependency;
the numbers here are retrieval-precision knobs, not model limits.
"""

import re
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

CHARS_PER_TOKEN = 4
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
#: tried in order; the first separator that splits an oversized block wins
SEPARATORS = ["\n\n", "\n", ". ", " "]


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


@dataclass
class Chunk:
    """One indexed unit: what gets embedded, plus how to cite it."""

    id: str
    #: text handed to the embedder, breadcrumb prefix included
    text: str
    #: original text without the prefix, used when building the answer prompt
    raw_text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class _Section:
    heading_path: str
    text: str


def _split_by_headings(text: str) -> list[_Section]:
    """Split markdown-ish text into sections, tracking the heading stack."""
    sections: list[_Section] = []
    stack: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append(_Section(" > ".join(stack), body))
        buffer.clear()

    for line in text.splitlines():
        match = HEADING_RE.match(line.strip())
        if match:
            flush()
            level = len(match.group(1))
            stack[:] = stack[: level - 1]
            stack.append(match.group(2).strip())
        else:
            buffer.append(line)
    flush()
    return sections or [_Section("", text.strip())]


def _split_oversized(text: str, limit: int, overlap: int) -> list[str]:
    """Recursively split `text` on the coarsest separator that fits."""
    if len(text) <= limit:
        return [text]

    for sep in SEPARATORS:
        if sep not in text:
            continue
        pieces, current = [], ""
        for part in text.split(sep):
            candidate = part if not current else current + sep + part
            if len(candidate) > limit and current:
                pieces.append(current)
                # Carry the tail of the previous piece for continuity -- but
                # only when the new part actually fits with it. Gluing overlap
                # onto an already-oversized part rebuilds a piece no smaller
                # than the input, which then recurses on itself forever.
                tail = current[-overlap:] if overlap else ""
                current = (
                    tail + sep + part
                    if tail and len(tail) + len(sep) + len(part) <= limit
                    else part
                )
            else:
                current = candidate
        if current:
            pieces.append(current)
        # A split where some piece is no shorter than the input has made no
        # progress; try the next separator rather than recursing on it.
        if len(pieces) > 1 and all(len(piece) < len(text) for piece in pieces):
            out: list[str] = []
            for piece in pieces:
                out.extend(_split_oversized(piece, limit, overlap) if len(piece) > limit else [piece])
            return out

    # no separator helped (e.g. one enormous unbroken token): hard-cut
    step = max(limit - overlap, 1)  # a >=limit overlap would never advance
    return [text[i : i + limit] for i in range(0, len(text), step)]


def _common_path(a: str, b: str) -> str:
    """Longest shared heading prefix of two paths -- the label for a merge."""
    left, right = a.split(" > "), b.split(" > ")
    shared: list[str] = []
    for x, y in zip(left, right):
        if x != y:
            break
        shared.append(x)
    return " > ".join(shared)


def chunk_document(
    *,
    doc_id: str,
    text: str,
    title: str,
    breadcrumb: str = "",
    metadata: dict | None = None,
    id_prefix: str | None = None,
) -> list[Chunk]:
    """Split one document into embeddable chunks with citation metadata.

    `id_prefix` becomes the stable chunk-id stem (see section 3.5); it should
    already encode the source, document id and version so that re-ingesting an
    unchanged document produces identical ids.
    """
    settings = get_settings()
    limit = settings.chunk_size * CHARS_PER_TOKEN
    overlap = settings.chunk_overlap * CHARS_PER_TOKEN
    base = dict(metadata or {})
    stem = id_prefix or doc_id

    if estimate_tokens(text) < settings.min_chunk_tokens:
        logger.info("Skipping %s: below the minimum document size", doc_id)
        return []

    pieces: list[tuple[str, str]] = []  # (heading_path, body)
    for section in _split_by_headings(text):
        for body in _split_oversized(section.text, limit, overlap):
            body = body.strip()
            if not body:
                continue
            # Pack short sections together rather than emitting slivers; a page
            # of many small headed sections must not fragment into noise.
            if pieces and len(pieces[-1][1]) + len(body) + 2 <= limit:
                prev_path, prev_body = pieces[-1]
                merged_path = prev_path if prev_path == section.heading_path else _common_path(prev_path, section.heading_path)
                pieces[-1] = (merged_path, f"{prev_body}\n\n{body}")
            else:
                pieces.append((section.heading_path, body))

    if not pieces:
        logger.info("Document %s produced no chunks", doc_id)
        return []

    chunks: list[Chunk] = []
    for index, (heading_path, body) in enumerate(pieces):
        trail = " > ".join(p for p in (breadcrumb, title, heading_path) if p)
        chunks.append(
            Chunk(
                id=f"{stem}:{index:04d}",
                text=f"[{trail}]\n{body}" if trail else body,
                raw_text=body,
                metadata={
                    **base,
                    "chunk_id": f"{stem}:{index:04d}",
                    "doc_id": doc_id,
                    "title": title,
                    "heading_path": heading_path,
                    "chunk_index": index,
                    "chunk_total": len(pieces),
                },
            )
        )
    return chunks
