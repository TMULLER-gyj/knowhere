"""Annotate parser-owned table serialization, never author-supplied provenance tags."""
from __future__ import annotations

from html.parser import HTMLParser

from shared.services.chunks.evidence_provenance import Provenance, ProvenanceText, join_text, marked


def annotate_table_html(html: str, *, text_kind: Provenance) -> ProvenanceText:
    """Keep exact asset bytes; markup/formatting is system, cell text has caller authority.

    Call only before summaries are inserted. Generic Markdown/PDF producers must
    pass unknown; a raw-text or Excel extraction boundary may pass source.
    """
    offsets = [0]
    for line in html.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    ranges: list[tuple[int, int]] = []

    class TextRanges(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=False)

        def record(self, value: str) -> None:
            line, column = self.getpos()
            start = offsets[line - 1] + column
            ranges.append((start, start + len(value)))

        def handle_data(self, data: str) -> None:
            if data.strip():
                self.record(data)

        def record_reference(self, prefix: str, name: str) -> None:
            line, column = self.getpos()
            start = offsets[line - 1] + column
            value = prefix + name
            if html[start + len(value):start + len(value) + 1] == ";":
                value += ";"
            self.record(value)

        def handle_entityref(self, name: str) -> None:
            self.record_reference("&", name)

        def handle_charref(self, name: str) -> None:
            self.record_reference("&#", name)

    parser = TextRanges()
    parser.feed(html)
    parser.close()
    parts = []
    offset = 0
    for start, end in ranges:
        parts.extend((marked(html[offset:start], "system"), marked(html[start:end], text_kind)))
        offset = end
    parts.append(marked(html[offset:], "system"))
    return join_text(parts)
