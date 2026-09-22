"""Deterministic text format interpretation; external resources fail closed."""

from __future__ import annotations

import base64
import csv
import io
import json
import re

from bs4 import BeautifulSoup
from bs4.element import Comment, NavigableString, Tag

from .export import CompleteSourceError, ExportBuilder, json_text

_UNSUPPORTED_HTML_CONTENT = {"iframe", "object", "embed", "svg", "canvas", "video", "audio", "picture", "input", "select", "textarea"}


def export_delimited(builder: ExportBuilder, text: str, extension: str) -> None:
    try:
        delimiter = "\t" if extension == ".tsv" else csv.Sniffer().sniff(text, delimiters=",;\t").delimiter
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True))
    except (csv.Error, ValueError) as exc:
        raise CompleteSourceError("complete-source-table-invalid") from exc
    if not rows or len(rows) > 100_000 or max(map(len, rows), default=0) < 2:
        raise CompleteSourceError("complete-source-table-invalid")
    builder.text("1", json_text({
        "format": "table-cells@v1", "delimiter": delimiter,
        "rows": [[{"column": column + 1, "type": "string", "value": value} for column, value in enumerate(row)] for row in rows],
    }))


def _image(builder: ExportBuilder, source: str) -> None:
    matched = re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,([A-Za-z0-9+/=\r\n]+)", source)
    if matched is None:
        raise CompleteSourceError("complete-source-external-resource-required")
    try:
        data = base64.b64decode(matched.group(1).replace("\r", "").replace("\n", ""), validate=True)
    except ValueError as exc:
        raise CompleteSourceError("complete-source-image-invalid") from exc
    builder.image("1", data)


def export_markdown(builder: ExportBuilder, text: str) -> None:
    # Preserve all text whitespace, replacing binary data carriers with actual
    # ordered image parts. Base64 prose is never the image input.
    position = 0
    parsed_image_count = 0
    for match in re.finditer(r"!\[([^\]]*)\]\(([^\s)]+)(?:\s+[^)]*)?\)", text):
        parsed_image_count += 1
        builder.text("1", text[position:match.start()])
        if match.group(1):
            builder.text("1", match.group(1))
        _image(builder, match.group(2))
        position = match.end()
    builder.text("1", text[position:])
    if len(re.findall(r"!\[[^\]]*\]", text)) != parsed_image_count or re.search(r"<(?:img|picture)\b", text, flags=re.I):
        raise CompleteSourceError("complete-source-external-resource-required")


def export_html(builder: ExportBuilder, text: str) -> None:
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup.find_all(["head", "script", "style", "noscript", "template"]):
        tag.decompose()
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag) or tag.decomposed:
            continue
        style = str(tag.get("style", "")).replace(" ", "").lower()
        if tag.has_attr("hidden") or "display:none" in style or "visibility:hidden" in style:
            tag.decompose()
        elif "url(" in style:
            raise CompleteSourceError("complete-source-external-resource-required")
    root = soup.body or soup
    if not root.get_text("", strip=True) and root.find("img") is None:
        raise CompleteSourceError("complete-source-empty")
    buffer: list[str] = []
    table_cells: dict[int, dict[str, int | str]] = {}

    def flush() -> None:
        value = "".join(buffer)
        buffer.clear()
        if value.strip():
            builder.text("1", value)

    def walk(node: Tag | NavigableString) -> None:
        if isinstance(node, NavigableString):
            if not isinstance(node, Comment):
                buffer.append(str(node))
            return
        if node.name in _UNSUPPORTED_HTML_CONTENT:
            raise CompleteSourceError("complete-source-unsupported-embedded-content")
        if node.name == "img":
            flush()
            if node.get("srcset"):
                raise CompleteSourceError("complete-source-external-resource-required")
            if node.get("alt"):
                builder.text("1", str(node.get("alt")))
            _image(builder, str(node.get("src", "")))
            return
        if node.name == "math":
            if node.find(list(_UNSUPPORTED_HTML_CONTENT | {"img"})) is not None:
                raise CompleteSourceError("complete-source-unsupported-embedded-content")
            buffer.append(json_text({"format": "mathml", "expression": str(node)}))
            return
        if node.name in {"td", "th"} and id(node) in table_cells:
            flush()
            builder.text("1", json_text({"format": "html-table-cell@v1", **table_cells[id(node)]}))
        if node.name == "table":
            flush()
            rows = []
            occupied: dict[tuple[int, int], bool] = {}
            if node.find("table") is not None:
                raise CompleteSourceError("complete-source-unsupported-nested-table")
            for row_index, row in enumerate(node.find_all("tr"), 1):
                if not isinstance(row, Tag):
                    continue
                cells = []
                column = 1
                for cell in row.find_all(["td", "th"], recursive=False):
                    if not isinstance(cell, Tag):
                        continue
                    try:
                        row_span = int(str(cell.get("rowspan", "1")))
                        column_span = int(str(cell.get("colspan", "1")))
                    except ValueError as exc:
                        raise CompleteSourceError("complete-source-table-invalid") from exc
                    if not 1 <= row_span <= 10_000 or not 1 <= column_span <= 10_000:
                        raise CompleteSourceError("complete-source-table-invalid")
                    if row_span * column_span > 100_000:
                        raise CompleteSourceError("complete-source-resource-limit")
                    while occupied.get((row_index, column)):
                        column += 1
                    cell_coordinate = {"row": row_index, "column": column, "type": "string", "row_span": row_span, "column_span": column_span}
                    cells.append(cell_coordinate)
                    table_cells[id(cell)] = cell_coordinate
                    for covered_row in range(row_index, row_index + row_span):
                        for covered_column in range(column, column + column_span):
                            if occupied.get((covered_row, covered_column)):
                                raise CompleteSourceError("complete-source-table-invalid")
                            occupied[(covered_row, covered_column)] = True
                    if len(occupied) > 100_000:
                        raise CompleteSourceError("complete-source-resource-limit")
                    column += column_span
                rows.append(cells)
            # Structure is a descriptor; actual caption/cell content follows in
            # source order through the SAME walker as the document body. A local
            # get_text/img shortcut would flatten math and bypass rejection of
            # unsupported visual content. V2 deliberately has no flattened value.
            builder.text("1", json_text({"format": "html-table@v2", "rows": rows}))
        block = node.name in {"div", "p", "section", "article", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "br", "hr", "caption", "td", "th", "tr"}
        if block:
            buffer.append("\n")
        for child in node.children:
            if isinstance(child, (Tag, NavigableString)):
                walk(child)
        if block:
            buffer.append("\n")

    walk(root)
    flush()


def export_literature(builder: ExportBuilder, text: str, extension: str) -> None:
    if extension == ".json":
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise CompleteSourceError("complete-source-literature-invalid") from exc
        csl_records = value if isinstance(value, list) else [value]
        if not csl_records:
            raise CompleteSourceError("complete-source-literature-invalid")
        for csl_record in csl_records:
            if not isinstance(csl_record, dict):
                raise CompleteSourceError("complete-source-literature-invalid")
            content_fields = [csl_record.get(key) for key in ("title", "abstract", "container-title")]
            if not any(isinstance(content, str) and content.strip() for content in content_fields):
                raise CompleteSourceError("complete-source-literature-invalid")
        builder.text("1", json_text({"format": "csl-json", "records": csl_records}))
        return
    if extension == ".ris":
        records: list[dict[str, list[str]]] = []
        record: dict[str, list[str]] | None = None
        field: str | None = None
        for line in text.splitlines():
            match = re.match(r"^([A-Z0-9]{2})  - ?(.*)$", line)
            if match:
                tag_key, value = match.groups()
                field = tag_key
                if field == "TY":
                    if record is not None:
                        raise CompleteSourceError("complete-source-literature-invalid")
                    record = {}
                if record is None:
                    raise CompleteSourceError("complete-source-literature-invalid")
                record.setdefault(tag_key, []).append(value)
                if field == "ER":
                    records.append(record)
                    record = None
            elif line.strip():
                if record is None or field is None:
                    raise CompleteSourceError("complete-source-literature-invalid")
                record[field][-1] += "\n" + line
        if record is not None or not records or any(not any(any(value.strip() for value in row.get(key, [])) for key in ("TI", "T1", "AB", "N2")) for row in records):
            raise CompleteSourceError("complete-source-literature-invalid")
        builder.text("1", json_text({"format": "ris", "records": records}))
        return
    if extension == ".bib":
        _validate_bibtex(text)
    builder.text("1", text)


def _validate_bibtex(text: str) -> None:
    """Parse entry/field structure; retain original syntax and macro evidence."""
    position = 0
    content_entries = 0
    while match := re.search(r"@(\w+)\s*([{(])", text[position:]):
        entry_type, opening = match.groups()
        start = position + match.end()
        closing = "}" if opening == "{" else ")"
        depth, quote, escaped, comment = 0, False, False, False
        fields: list[str] = []
        field_start = start
        end = None
        for index in range(start, len(text)):
            char = text[index]
            if comment:
                comment = char not in "\r\n"
                continue
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif char == "%" and depth == 0 and not quote:
                comment = True
            elif char == '"' and depth == 0:
                quote = not quote
            elif not quote and char == "{":
                depth += 1
            elif not quote and depth == 0 and char == closing:
                fields.append(text[field_start:index].strip())
                end = index + 1
                break
            elif not quote and char == "}":
                depth -= 1
                if depth < 0:
                    raise CompleteSourceError("complete-source-literature-invalid")
            elif not quote and depth == 0 and char == ",":
                fields.append(text[field_start:index].strip())
                field_start = index + 1
        if end is None:
            raise CompleteSourceError("complete-source-literature-invalid")
        if entry_type.lower() not in {"comment", "string", "preamble"}:
            if not fields or not fields[0] or "=" in fields[0]:
                raise CompleteSourceError("complete-source-literature-invalid")
            names: set[str] = set()
            meaningful = False
            for field in fields[1:]:
                if not field:
                    continue
                parsed_field = re.fullmatch(r"\s*([\w-]+)\s*=\s*(.+)", field, flags=re.S)
                if parsed_field is None:
                    raise CompleteSourceError("complete-source-literature-invalid")
                name, value = parsed_field.groups()
                name = name.lower()
                if name in names:
                    raise CompleteSourceError("complete-source-literature-invalid")
                names.add(name)
                if name in {"title", "abstract"} and value.strip(' {}"\t\r\n'):
                    meaningful = True
            if not meaningful:
                raise CompleteSourceError("complete-source-literature-invalid")
            content_entries += 1
        position = end
    if content_entries == 0:
        raise CompleteSourceError("complete-source-literature-invalid")
