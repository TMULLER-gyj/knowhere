"""Compose retrieval evidence parts from raw chunks and connected assets.

Text and standalone image/table chunks place table HTML and image bytes at
path placeholders. Page chunks are summary plus the page image; they do not
inline connected charts.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger
from shared.services.chunks.evidence_provenance import decode_provenance, slice_text

from shared.services.retrieval.hydration.asset_inline import (
    remove_path_placeholders,
)
from shared.services.retrieval.hydration.row_utils import (
    extract_page_nums,
    normalize_chunk_type,
    page_summary,
)
from shared.services.retrieval.hydration.table_grid import (
    TableDownloadError,
    load_table_html,
)
from shared.services.storage.result_storage import get_result_storage

_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def compose_evidence_parts(
    row: dict[str, Any],
    rows_by_chunk_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    chunk_type = normalize_chunk_type(row.get("chunk_type"))
    if chunk_type == "page":
        return _compose_page_parts(row)
    if chunk_type == "table":
        return _compose_standalone_table_parts(row)
    if chunk_type == "image":
        return _compose_standalone_image_parts(row)
    return _compose_text_parts(row, rows_by_chunk_id)


def collect_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for row in rows:
        composed = row.get("composed")
        if isinstance(composed, list):
            # Attribute composed assets to the result row that contains them, not
            # to an embedded child omitted from public results. Copy rather than
            # mutate the hydration-owned parts.
            parts.extend(
                {**part, "chunk_id": row["chunk_id"], "document_id": row["document_id"]}
                for part in composed
            )
    return parts


def flatten_parts(parts: list[dict[str, Any]] | None) -> str:
    texts: list[str] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = str(part.get("text") or "")
            if text:
                texts.append(text)
            continue
        if part.get("type") != "image":
            continue
        media_type = str(part.get("media_type") or "").strip() or "application/octet-stream"
        data = str(part.get("data") or "").strip()
        if data:
            texts.append(f"data:{media_type};base64,{data}")
    return "".join(texts)


def _compose_page_parts(row: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    summary = page_summary(row)
    if summary:
        parts.append(_text_part(summary, "generated-summary"))
    image, warning = _try_read_page_image(row)
    if image is not None:
        parts.append(image)
    if warning:
        parts.append(_text_part(f"Page image unavailable: {warning}", "system"))
    return parts


def _compose_standalone_table_parts(row: dict[str, Any]) -> list[dict[str, Any]]:
    html = _try_read_table_html(row)
    if html is None:
        return []
    return _bound_parts(html, row, "table_evidence_provenance")


def _compose_standalone_image_parts(row: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    description = str(row.get("content") or "")
    if description:
        parts.extend(_bound_parts(description, row))
    image = _try_read_image(row)
    if image is not None:
        parts.append(image)
    return parts


def _compose_text_parts(
    row: dict[str, Any],
    rows_by_chunk_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    content = str(row.get("content") or "")
    annotated = _bound_text(content, row)
    tables, images = _embed_targets(row, rows_by_chunk_id)
    # Locate only parser asset references, never generated prose. Slice the
    # validated interval stream before replacing placeholders with child parts.
    targets = tables + images
    parts: list[dict[str, Any]] = []
    offset = 0
    while targets:
        matches = [(content.find(candidate, offset), -len(candidate), target, candidate)
                   for target in targets for candidate in _ref_candidates(target[2])
                   if candidate and content.find(candidate, offset) >= 0]
        if not matches:
            break
        index, _, target, candidate = min(matches, key=lambda item: (item[0], item[1]))
        parts.extend(_segment_parts(slice_text(annotated, offset, index)))
        target_row = target[1]
        if target in tables:
            html = _try_read_table_html(target_row)
            if html is not None:
                parts.append(_text_part("\n", "system"))
                parts.extend(_bound_parts(html, target_row, "table_evidence_provenance"))
                parts.append(_text_part("\n", "system"))
        else:
            image = _try_read_image(target_row)
            if image is not None:
                parts.extend([_text_part("\n", "system"), image, _text_part("\n", "system")])
        offset = index + len(candidate)
        targets.remove(target)
    parts.extend(_segment_parts(slice_text(annotated, offset, len(content))))
    return [cleaned for part in parts
            if not _is_empty_text_part(cleaned := _clean_text_part(part))]


def _bound_text(text, row, key="evidence_provenance"):
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    return decode_provenance(text, metadata.get(key) if isinstance(metadata, dict) else None)


def _segment_parts(text):
    return [_text_part(value, kind) for value, kind in text.segments]


def _bound_parts(text, row, key="evidence_provenance"):
    return _segment_parts(_bound_text(text, row, key))


def _embed_targets(
    row: dict[str, Any],
    rows_by_chunk_id: dict[str, dict[str, Any]],
) -> tuple[
    list[tuple[str, dict[str, Any], str]],
    list[tuple[str, dict[str, Any], str]],
]:
    tables: list[tuple[str, dict[str, Any], str]] = []
    images: list[tuple[str, dict[str, Any], str]] = []
    for item in _connections(row):
        if item.get("relation") != "embeds":
            continue
        target_id = str(item.get("target") or "").strip()
        ref = str(item.get("ref") or "").strip()
        target_row = rows_by_chunk_id.get(target_id)
        if not target_id or not ref or target_row is None:
            continue
        target_type = normalize_chunk_type(target_row.get("chunk_type"))
        if target_type == "table":
            tables.append((target_id, target_row, ref))
        elif target_type == "image":
            images.append((target_id, target_row, ref))
    return tables, images


def _connections(row: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return []
    connections = metadata.get("connect_to") or []
    if not isinstance(connections, list):
        return []
    return [item for item in connections if isinstance(item, dict)]


def _ref_candidates(ref: str) -> list[str]:
    raw = str(ref or "").strip()
    if not raw:
        return []
    out = [raw]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if inner and inner not in out:
            out.append(inner)
    else:
        bracketed = f"[{raw}]"
        if bracketed not in out:
            out.append(bracketed)
    return out


def _try_read_table_html(row: dict[str, Any]) -> str | None:
    try:
        html = load_table_html(row)
    except TableDownloadError as exc:
        _warn_skipped(row, "table", str(exc))
        return None
    if html:
        return html
    _warn_skipped(row, "table", "missing table HTML")
    return None


def _try_read_image(row: dict[str, Any]) -> dict[str, Any] | None:
    artifact = str(row.get("file_path") or "").strip()
    return _try_read_image_artifact(row, artifact, media_type=_media_type_from_path(artifact))


def _try_read_page_image(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    asset, warning = _select_page_asset(row)
    if asset is None:
        reason = warning or "missing page image"
        _warn_skipped(row, "image", reason)
        return None, reason
    artifact = str(asset.get("artifact_ref") or "").strip()
    media_type = (
        str(asset.get("content_type") or "").split(";", 1)[0].strip()
        or _media_type_from_path(artifact)
    )
    image = _try_read_image_artifact(row, artifact, media_type=media_type)
    if image is None:
        return None, "could not read page image"
    return image, None


def _try_read_image_artifact(
    row: dict[str, Any],
    artifact: str,
    *,
    media_type: str,
) -> dict[str, Any] | None:
    job_id = str(row.get("job_id") or "").strip()
    storage = get_result_storage()
    normalized = storage.normalize_artifact_ref(artifact)
    if not job_id or not normalized:
        _warn_skipped(row, "image", "missing artifact")
        return None
    try:
        temp_path = storage.download_raw_to_temp(
            job_id=job_id,
            relative_path=normalized,
            suffix=Path(normalized).suffix or ".bin",
            temp_dir=tempfile.gettempdir(),
        )
        body = Path(temp_path).read_bytes()
    except Exception as exc:
        _warn_skipped(row, "image", str(exc))
        return None
    if not body:
        _warn_skipped(row, "image", "empty image bytes")
        return None
    return {
        "type": "image",
        "provenance": "source",
        "media_type": media_type,
        "data": base64.b64encode(body).decode("ascii"),
    }


def _select_page_asset(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    metadata = row.get("chunk_metadata") or row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None, "missing page image"
    assets = metadata.get("page_assets") or []
    if not isinstance(assets, list):
        return None, "missing page image"
    candidates = [item for item in assets if isinstance(item, dict)]
    if not candidates:
        return None, "missing page image"
    page_nums = extract_page_nums(row) or []
    if not page_nums:
        return None, "missing page number"
    for item in candidates:
        raw_page_num = item.get("page_num")
        if raw_page_num is None:
            continue
        try:
            page_num = int(raw_page_num)
        except (TypeError, ValueError):
            continue
        if page_num in page_nums:
            return item, None
    return None, "page image does not match this page"


def _media_type_from_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return _IMAGE_MEDIA_TYPES.get(suffix, "application/octet-stream")


def _text_part(text: str, provenance: str = "unknown") -> dict[str, Any]:
    return {"type": "text", "text": text, "provenance": provenance}


def _clean_text_part(part: dict[str, Any]) -> dict[str, Any]:
    if part.get("type") != "text":
        return part
    return {**part, "text": remove_path_placeholders(str(part.get("text") or ""))}


def _is_empty_text_part(part: dict[str, Any]) -> bool:
    return part.get("type") == "text" and not str(part.get("text") or "")


def _warn_skipped(row: dict[str, Any], kind: str, reason: str) -> None:
    logger.warning(
        "retrieval: skipped unreachable evidence asset type={} ref={} asset_url={} source_path={} reason={}",
        kind,
        row.get("chunk_id"),
        row.get("asset_url"),
        row.get("file_path"),
        reason,
    )
