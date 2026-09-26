from __future__ import annotations

from app.services.document_parser.support.parser_rows import ParsedRow
from shared.services.chunks.evidence_provenance import ProvenanceText, text_metadata


def build_image_asset_row(
    *,
    content: str,
    relative_path: str,
    summary: str,
    know_id: str,
    addtime: str,
    keywords: str = "",
    entities: str = "",
    asset_title: str = "",
) -> ParsedRow:
    content_value = content if isinstance(content, ProvenanceText) else str(content)
    return ParsedRow(
        content=content_value,
        extra_metadata=text_metadata(content_value) if isinstance(content_value, ProvenanceText) else None,
        path=relative_path,
        type="image",
        keywords=keywords,
        summary=summary,
        know_id=know_id,
        tokens="",
        connectto="",
        addtime=addtime,
        entities=entities,
        asset_title=asset_title,
    )


def build_table_asset_row(
    *,
    relative_path: str,
    summary: str,
    keywords: str,
    know_id: str,
    addtime: str,
    entities: str = "",
    asset_title: str = "",
    image_refs: list[str] | None = None,
    extra_metadata: dict | None = None,
) -> ParsedRow:
    row_content = relative_path
    # Multiline type channel carries table→image embeds
    # (same pattern as PTXT\n[tables/...] for text rows).
    type_value = "table"
    if image_refs:
        type_value = "\n".join(["table", *image_refs])
    return ParsedRow(
        content=row_content,
        extra_metadata=extra_metadata,
        path=relative_path,
        type=type_value,
        keywords=keywords,
        summary=summary,
        know_id=know_id,
        tokens="",
        connectto="",
        addtime=addtime,
        length=len(row_content),
        entities=entities,
        asset_title=asset_title,
    )
