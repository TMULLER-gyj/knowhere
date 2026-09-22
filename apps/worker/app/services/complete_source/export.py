"""Complete, job-bound evidence export. Never calls a model or retrieval parser."""

from __future__ import annotations

import hashlib
import io
import json
import warnings
import zipfile
from pathlib import Path
from typing import Any, Literal

from PIL import Image

from shared.models.schemas.complete_source import (
    COMPLETE_SOURCE_EXTENSIONS,
    CompleteSourceCoverage,
    CompleteSourceImagePart,
    CompleteSourceManifest,
    CompleteSourceRequest,
    CompleteSourceResource,
    CompleteSourceTextPart,
)

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_UNITS = 500
MAX_PARTS = 20_000


class CompleteSourceError(ValueError):
    """Secret-free stable failure code; no user bytes or filenames in errors."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ExportBuilder:
    def __init__(self) -> None:
        self.parts: list[CompleteSourceTextPart | CompleteSourceImagePart] = []
        self.resources: list[CompleteSourceResource] = []
        self.resource_bytes: dict[str, bytes] = {}
        self.byte_length = 0

    def _reserve(self, size: int) -> None:
        self.byte_length += size
        if self.byte_length > MAX_EXPORT_BYTES or len(self.parts) >= MAX_PARTS:
            raise CompleteSourceError("complete-source-resource-limit")

    def text(self, unit: str, text: str) -> None:
        if not text:
            return
        raw = text.encode("utf-8")
        self._reserve(len(raw))
        self.parts.append(CompleteSourceTextPart(unit=unit, text=text, sha256=digest(raw)))

    def image(self, unit: str, data: bytes) -> None:
        if len(data) > MAX_INPUT_BYTES:
            raise CompleteSourceError("complete-source-resource-limit")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    if image.width * image.height > MAX_IMAGE_PIXELS:
                        raise CompleteSourceError("complete-source-resource-limit")
                    if getattr(image, "n_frames", 1) != 1:
                        raise CompleteSourceError("complete-source-unsupported-animation")
                    image.load()
                    width, height = image.size
                    extension = {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp"}.get(image.format or "")
                    if extension is None:
                        # Office may embed other raster formats: lossless pixels, no resize.
                        output = io.BytesIO()
                        image.convert("RGBA").save(output, format="PNG")
                        data, extension = output.getvalue(), "png"
        except CompleteSourceError:
            raise
        except Exception as exc:
            raise CompleteSourceError("complete-source-image-invalid") from exc
        self._reserve(len(data))
        path = f"resources/image-{len(self.resources) + 1}.{extension}"
        media_type: Literal["image/png", "image/jpeg", "image/webp"] = (
            "image/png" if extension == "png" else "image/jpeg" if extension == "jpeg" else "image/webp"
        )
        self.resources.append(CompleteSourceResource(
            path=path, media_type=media_type, sha256=digest(data),
            byte_length=len(data), width=width, height=height,
        ))
        self.resource_bytes[path] = data
        self.parts.append(CompleteSourceImagePart(unit=unit, resource_path=path))


def decode_text(data: bytes, encoding: str | None) -> str:
    try:
        if encoding:
            text = data.decode(encoding)
        elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-8-sig")
    except (UnicodeError, LookupError) as exc:
        raise CompleteSourceError("complete-source-encoding-required") from exc
    if not text.strip():
        raise CompleteSourceError("complete-source-empty")
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text) or "\ufffd" in text:
        raise CompleteSourceError("complete-source-text-invalid")
    return text


def _pdf(builder: ExportBuilder, data: bytes) -> CompleteSourceCoverage:
    import pymupdf

    if not data.startswith(b"%PDF-"):
        raise CompleteSourceError("complete-source-signature-invalid")
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                raise CompleteSourceError("complete-source-encrypted")
            if not 1 <= len(document) <= MAX_UNITS:
                raise CompleteSourceError("complete-source-resource-limit")
            units = [str(index + 1) for index in range(len(document))]
            text_pages = 0
            for index, unit in enumerate(units):
                page = document.load_page(index)
                # Native text is supplemental. Every page is rendered, including TOC/scan/blank.
                text = page.get_text("text", sort=False)
                if not isinstance(text, str):
                    raise CompleteSourceError("complete-source-pdf-invalid")
                if text.strip():
                    text_pages += 1
                    builder.text(unit, text)
                if page.rect.width * page.rect.height * 4 > MAX_IMAGE_PIXELS:
                    raise CompleteSourceError("complete-source-resource-limit")
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                builder.image(unit, pixmap.tobytes("png"))
            return CompleteSourceCoverage(
                unit_kind="page", total_units=len(units), covered_units=units,
                text_extraction="native" if text_pages == len(units) else "unavailable" if not text_pages else "mixed",
                vision_required=True,
            )
    except CompleteSourceError:
        raise
    except Exception as exc:
        raise CompleteSourceError("complete-source-pdf-invalid") from exc


def build_complete_source(
    *, job_id: str, source_path: Path, request: CompleteSourceRequest, output_path: Path,
) -> tuple[CompleteSourceManifest, str, int]:
    if not 0 < source_path.stat().st_size <= MAX_INPUT_BYTES:
        raise CompleteSourceError("complete-source-resource-limit")
    data = source_path.read_bytes()
    extension = source_path.suffix.lower()
    if extension not in COMPLETE_SOURCE_EXTENSIONS[request.kind]:
        raise CompleteSourceError("complete-source-unsupported-format")
    builder = ExportBuilder()
    extraction = "decoded"
    if extension == ".pdf" and request.kind == "document":
        coverage = _pdf(builder, data)
    else:
        units, unit_kind = ["1"], "document"
        if request.kind == "figure":
            expected = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".webp": b"RIFF"}[extension]
            if not data.startswith(expected) or (extension == ".webp" and data[8:12] != b"WEBP"):
                raise CompleteSourceError("complete-source-signature-invalid")
            builder.image("1", data)
            extraction = "unavailable"
        elif extension in {".xlsx", ".docx"}:
            from .office import export_docx, export_xlsx
            if extension == ".xlsx":
                units = export_xlsx(builder, data)
                unit_kind = "sheet"
            else:
                export_docx(builder, data)
            extraction = "structured"
        else:
            if data.startswith((b"%PDF-", b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
                raise CompleteSourceError("complete-source-signature-invalid")
            text = decode_text(data, request.encoding)
            if request.kind == "code" or request.kind in {"text", "equation"}:
                builder.text("1", text)
            elif extension in {".html", ".htm"}:
                from .structured_text import export_html
                export_html(builder, text)
                extraction = "structured"
            elif request.kind == "table":
                from .structured_text import export_delimited
                export_delimited(builder, text, extension)
                extraction = "structured"
            elif request.kind == "literature":
                from .structured_text import export_literature
                export_literature(builder, text, extension)
                extraction = "structured"
            else:
                from .structured_text import export_markdown
                export_markdown(builder, text)
        coverage = CompleteSourceCoverage(
            unit_kind=unit_kind, total_units=len(units), covered_units=units,
            text_extraction=extraction, vision_required=bool(builder.resources),
        )
    if not builder.parts or any(not any(part.unit == unit and (part.type == "image" or part.text.strip()) for part in builder.parts) for unit in coverage.covered_units):
        raise CompleteSourceError("complete-source-partial")
    manifest = CompleteSourceManifest(
        job_id=job_id, input_sha256=digest(data), input_byte_length=len(data),
        kind=request.kind, coverage=coverage, parts=builder.parts, resources=builder.resources,
    )
    entries = {"complete-source.json": manifest.model_dump_json().encode("utf-8"), **builder.resource_bytes}
    if sum(map(len, entries.values())) > MAX_EXPORT_BYTES:
        raise CompleteSourceError("complete-source-resource-limit")
    # Deterministic metadata; STORED avoids decompression amplification at consumers.
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in entries.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), value)
    result = output_path.read_bytes()
    return manifest, digest(result), len(result)
