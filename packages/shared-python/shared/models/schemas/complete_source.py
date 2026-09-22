"""Versioned, closed contract for complete source exports (not retrieval chunks)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SourceKind = Literal["text", "figure", "table", "equation", "code", "literature", "document"]
SourceEncoding = Literal["utf-8", "utf-16", "utf-16le", "utf-16be", "gb18030", "big5", "windows-1252", "shift_jis"]


class CompleteSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["knowhere-complete-source-request@v1"]
    kind: SourceKind
    encoding: SourceEncoding | None = None


COMPLETE_SOURCE_EXTENSIONS: dict[str, frozenset[str]] = {
    "text": frozenset({".txt"}),
    "figure": frozenset({".jpg", ".jpeg", ".png", ".webp"}),
    "table": frozenset({".csv", ".tsv", ".xlsx", ".html", ".htm"}),
    "equation": frozenset({".tex"}),
    "code": frozenset({
        ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
        ".java", ".js", ".json", ".kt", ".kts", ".lua", ".md", ".php", ".py",
        ".r", ".rb", ".rs", ".sh", ".sql", ".svelte", ".swift", ".toml", ".ts",
        ".tsx", ".vue", ".xml", ".yaml", ".yml",
    }),
    "literature": frozenset({".bib", ".json", ".ris", ".txt"}),
    "document": frozenset({".docx", ".htm", ".html", ".md", ".pdf", ".txt"}),
}


class CompleteSourceTextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"] = "text"
    unit: str
    text: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompleteSourceImagePart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["image"] = "image"
    unit: str
    resource_path: str


class CompleteSourceResource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class CompleteSourceCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["complete"] = "complete"
    unit_kind: Literal["page", "sheet", "document"]
    total_units: int = Field(gt=0)
    covered_units: list[str]
    text_extraction: Literal["native", "decoded", "structured", "unavailable", "mixed"]
    vision_required: bool


class CompleteSourceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["knowhere-complete-source@v1"] = "knowhere-complete-source@v1"
    job_id: str
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_byte_length: int = Field(gt=0)
    kind: SourceKind
    coverage: CompleteSourceCoverage
    parts: list[CompleteSourceTextPart | CompleteSourceImagePart]
    resources: list[CompleteSourceResource]
