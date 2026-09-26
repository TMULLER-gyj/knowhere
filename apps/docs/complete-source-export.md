# Complete source export v1

`POST /api/v2/jobs` supports an opt-in, **file-only** complete-source export.
Requests without `complete_source` keep their existing parser/retrieval behavior.
This mode does not invoke LLMs, MinerU, Java, LibreOffice, or a vector/retrieval
pipeline. It needs the API, Worker, database, Redis, object storage and installed
PyMuPDF/Pillow/Office Python libraries. Existing Job admission and billing apply.

```json
{
  "source_type": "file",
  "file_name": "unique-attempt.pdf",
  "namespace": "source-parsing",
  "complete_source": {
    "schema_version": "knowhere-complete-source-request@v1",
    "kind": "document"
  }
}
```

The closed source kinds are `text`, `figure`, `table`, `equation`, `code`,
`literature`, and `document`. Optional `encoding` selects `utf-8`, `utf-16`,
`utf-16le`, `utf-16be`, `gb18030`, `big5`, `windows-1252`, or `shift_jis`. By default,
UTF-8 and BOM-marked UTF-16 are accepted strictly; undecodable text requires
an explicit encoding. Source URLs, existing-document updates and LLM overrides
are rejected. Complete exports have `document_id: null`: there is a Job Result,
not a new retrieval Document.

Use returned `upload_url` and `upload_headers` for the exact file bytes. While
waiting-file, authenticated GET Job returns newly signed upload instructions
for the Job's own stored object key (complete-source and ordinary file Jobs
alike); no other user's Job can be read. Confirm the upload
with the existing confirm-upload endpoint, then poll the same Job. After `done`,
download `result_url` and verify SHA-256 against `result.checksum`. Never forward
the Knowhere bearer credential to object storage or save signed URLs as identity.

## Result

The deterministic ZIP contains `complete-source.json` and all listed `resources/`
members, without debug files. JSON fields are defined by
`shared.models.schemas.complete_source.CompleteSourceManifest`:

- `schema_version: "knowhere-complete-source@v1"`, immutable `job_id`, `kind`,
  `input_sha256`, `input_byte_length`;
- `coverage`: `status: "complete"`, `unit_kind` (`page`, `sheet`, `document`),
  `total_units`, ordered unique `covered_units`, `text_extraction` (`native`,
  `decoded`, `structured`, `unavailable`, `mixed`), `vision_required`;
- ordered `parts`: `{type:"text", unit, text, sha256}` or
  `{type:"image", unit, resource_path}`;
- `resources`: `{path, media_type, sha256, byte_length, width, height}`.

Text SHA-256 covers exact UTF-8 bytes; image hashes cover exact resource bytes.
Page units are `"1"` through the physical page count. Workbook units are sheet
names in workbook order, including hidden sheets. A single document uses `"1"`.
Every resource is present and referenced. Images are required input, not optional
decorations: consumers need actual multimodal message parts and must reject
insufficient model/image/context budgets instead of discarding pages.

## Fidelity and explicit boundaries

| Format | Export |
| --- | --- |
| Text/code/LaTeX | Exact decoded whitespace and line structure. Code extensions use literal text even for HTML/JSON/Markdown. |
| Markdown document | Exact non-image text; embedded data images become ordered real image parts. Unavailable relative/remote image references fail. |
| PDF | Every page at 144 DPI, including TOC, tail, blank and scanned pages, plus available native text. No OCR is claimed for a scan. |
| JPEG/PNG/WebP | Decoded, verified actual image bytes. Animated sources fail. |
| CSV/TSV/semicolon text | Quoting and quoted newlines retained in typed row/cell JSON text. Suspicious single-column input fails. |
| XLSX | Sheet/coordinate/type/value/number-format/merge data, formulas and available cached values, all worksheets and embedded raster images. No formula evaluation. |
| DOCX | Ordered paragraphs/tables/images, tabs/line breaks, table merge descriptors, ancillary headers/footers/notes; OMML expressions are retained as structured XML evidence. Plain text boxes (DrawingML `wps` rectangle/text-box geometry or legacy VML `v:rect`/`v:shape` text boxes, e.g. Word's page-number footer) export their paragraphs in place under a `[TEXTBOX]` marker; any other shape geometry, connector, group, SmartArt or chart still fails explicitly. Font-specific `w:sym` glyphs fail explicitly rather than being guessed or dropped. |
| HTML | Visible body text, preformatted whitespace, table captions/cell coordinates/spans, MathML structure and data images. All table descendants use the same content/rejection traversal as the body. Head/scripts/styles/hidden subtrees are not body content. |
| Literature | CSL JSON/RIS fields and complete BibTeX/abstract evidence; id-only or malformed serialized records fail. |

HTML requires a self-contained body: no fetches of remote/relative images,
frames, objects, SVG, canvas, media or dynamic rendering. Unsupported embedded
Office objects/charts and external images fail explicitly. DOC/XLS/PPTX/GIF/SVG
are not newly admitted by this mode. These cases do not silently degrade into
text-only complete exports. A complete export proves the documented source
representation, not perfect model understanding or original Office pagination.

Limits: 20 MiB input; 64 MiB total export; 16 million decoded pixels per image;
500 PDF pages/workbook sheets; 20,000 parts; 100,000 workbook cells. Office ZIPs
are scanned before inflation: at most 2,048 entries, 64 MiB declared total,
16 MiB per entry and at most 100:1 for entries of at least 1 MiB. Traversal,
duplicate entries, encrypted members, DTD/entities, macros and external workbook
links are rejected. Consumer budgets may be lower.

Failure has no successful bundle: Job error `INVALID_ARGUMENT` includes a stable
`complete-source-*` reason (`partial`, `resource-limit`, `encoding-required`,
`unsupported-*`, `external-resource-required`, or format-invalid). A completed
Job is fenced from worker redelivery by the existing terminal-state gate.
Browser abort does not cancel Worker execution; archive does not erase files.
This mode adds no remote deletion or retention promise.

HTML table structure is emitted as a `html-table@v2` text descriptor followed by
caption/body content in original order. Each cell starts with its
`html-table-cell@v1` coordinate/span descriptor and then its actual text, MathML
or image parts. Descriptors do not replace content or flatten equations into
plain `get_text()` values. Unsupported graphics fail even inside captions/cells.

A failed complete-source Job exposes the same safe reason in `error.message` and
`error.details.violations[0].description` (`field: "complete_source"`), with
`error.code: "INVALID_ARGUMENT"` and `error.request_id` equal to the Job ID.
For example, undecodable bytes produce `complete-source-encoding-required`.
Consumers should map a closed allowlist of reasons, not display arbitrary upstream
messages. This is the public failure projection, not the internal exception text.

Both API and Worker must be deployed together. Source-level deterministic tests
do not prove that an already-running service has this version; verify the actual
upload → worker → ZIP path separately with an authorized, non-sensitive fixture.
