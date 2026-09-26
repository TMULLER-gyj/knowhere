"""Parser-owned provenance, bound to exact Unicode text (never inferred from prose)."""
from __future__ import annotations

import hashlib
from typing import Literal

Provenance = Literal["source", "generated-image-description", "generated-summary", "transcription", "system", "unknown"]
KINDS = frozenset({"source", "generated-image-description", "generated-summary", "transcription", "system", "unknown"})
VERSION = "evidence-provenance@v1"


class ProvenanceText(str):
    """Transient string-compatible segments retained by parser list chunking.

    Ordinary string operations deliberately drop the annotation. Callers must use
    join_text/slice_text at assembly boundaries; lost annotations decode unknown.
    """
    segments: tuple[tuple[str, Provenance], ...]

    def __new__(cls, segments: tuple[tuple[str, Provenance], ...]):
        obj = super().__new__(cls, "".join(text for text, _ in segments))
        obj.segments = tuple((text, kind) for text, kind in segments if text)
        return obj


def marked(text: str, kind: Provenance) -> ProvenanceText:
    return ProvenanceText(((text, kind),))


def join_text(items, separator: str = "") -> ProvenanceText:
    segments = []
    for index, item in enumerate(items):
        if index and separator:
            segments.append((separator, "system"))
        segments.extend(item.segments if isinstance(item, ProvenanceText) else ((str(item), "unknown"),))
    return ProvenanceText(tuple(segments))


def slice_text(text: ProvenanceText, start: int, end: int) -> ProvenanceText:
    segments = []
    offset = 0
    for value, kind in text.segments:
        left, right = max(start, offset), min(end, offset + len(value))
        if left < right:
            segments.append((value[left - offset:right - offset], kind))
        offset += len(value)
    return ProvenanceText(tuple(segments))


def replace_text(text: ProvenanceText, old: str, replacement: ProvenanceText) -> ProvenanceText:
    """Replace an explicit parser token while retaining surrounding provenance."""
    if not old:
        raise ValueError("Empty provenance replacement token")
    parts = []
    offset = 0
    while (index := text.find(old, offset)) >= 0:
        parts.extend((slice_text(text, offset, index), replacement))
        offset = index + len(old)
    parts.append(slice_text(text, offset, len(text)))
    return join_text(parts)


def strip_text(text: ProvenanceText) -> ProvenanceText:
    return slice_text(text, len(text) - len(text.lstrip()), len(text.rstrip()))


def encode_provenance(text: ProvenanceText) -> dict:
    offset = 0
    spans = []
    for value, kind in text.segments:
        spans.append({"start": offset, "end": offset + len(value), "provenance": kind})
        offset += len(value)
    return {"version": VERSION, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "spans": spans}


def decode_provenance(text: str, value: object) -> ProvenanceText:
    """Validate the entire binding before publishing any source-labelled text."""
    unknown = marked(text, "unknown")
    if not isinstance(value, dict) or set(value) != {"version", "sha256", "spans"}:
        return unknown
    if value["version"] != VERSION or value["sha256"] != hashlib.sha256(text.encode("utf-8")).hexdigest():
        return unknown
    spans = value["spans"]
    if not isinstance(spans, list):
        return unknown
    offset = 0
    segments = []
    for span in spans:
        if not isinstance(span, dict) or set(span) != {"start", "end", "provenance"}:
            return unknown
        start, end, kind = span["start"], span["end"], span["provenance"]
        if type(start) is not int or type(end) is not int or start != offset or not start < end <= len(text):
            return unknown
        if not isinstance(kind, str) or kind not in KINDS:
            return unknown
        segments.append((text[start:end], kind))
        offset = end
    return ProvenanceText(tuple(segments)) if offset == len(text) else unknown


def text_metadata(text: ProvenanceText) -> dict:
    return {"evidence_provenance": encode_provenance(text)}
