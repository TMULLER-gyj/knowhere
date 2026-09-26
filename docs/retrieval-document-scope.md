# Retrieval document scope

Both `POST /api/v1/retrieval/query` and `POST /api/v2/retrieval/query` accept
`include_document_ids` and `exclude_document_ids`.

| Request field | Meaning |
| --- | --- |
| `include_document_ids` omitted or `null` | All otherwise accessible documents in the requested namespace are eligible. |
| `include_document_ids: []` | No documents are eligible; retrieval returns empty results. |
| `include_document_ids: ["doc_a", "doc_b"]` | Only those documents are eligible. |
| `exclude_document_ids: ["doc_b"]` | Exclude these documents, including when they also appear in the include list. |
| `exclude_document_ids` omitted or `[]` | No additional document exclusions. |

For example:

```json
{
  "namespace": "default",
  "query": "What are the findings?",
  "include_document_ids": ["doc_a", "doc_b"],
  "exclude_document_ids": ["doc_b"]
}
```

Only `doc_a` is eligible. IDs must be document IDs, not filenames. Unknown,
foreign-user, and other-namespace IDs do not grant access. Duplicate include
IDs do not broaden the scope. An inclusion list that leaves no eligible
documents returns empty results and does not fall back to the full corpus.

The same boundary applies to small-corpus retrieval, classic search
(`use_agentic: false`), and agent exploration. Agent tools may select a narrower
set of documents, but cannot broaden the request boundary. Final references,
results, and connected asset hydration obey the same scope. Cache entries
distinguish unrestricted, empty, and explicitly included document sets.

Scope restricts available evidence; it does not add an LLM routing step or
change path filtering, ranking, or threshold semantics. Omitting both fields
preserves unrestricted retrieval within the existing user/namespace boundary.

## Evidence attribution

HTTP v1/v2 and MCP `retrieval.query` return additive `chunk_id` and
`document_id` fields on every `evidence` part (text, table HTML, image, and
page evidence). Match both fields to `results[].chunk_id` and
`results[].source.document_id`: content-derived chunk IDs alone are not
unique across documents. Embedded table/image parts belong to the containing
result row, not an omitted child row. Attribution does not certify that text
is original: page evidence may be generated summaries and image descriptions
may be generated text.

Part order, text/image payloads, `evidence_text`, `results`, and
`referenced_chunks` semantics are unchanged. Consumers allowing additive fields
remain compatible; strict part decoders must admit the two fields. HTTP and
MCP projections preserve them. The query cache shape is versioned to avoid
serving pre-attribution entries after deployment; no namespace data is deleted.

### Provenance extension (implementation under verification; live gate pending)

Attribution above is not a source-originality guarantee. The additive
`provenance` field distinguishes `source`, `generated-image-description`,
`generated-summary`, `transcription`, `system`, and `unknown`. This extension
must not be advertised as available until parser-to-public-response tests and
bounded live verification pass.

Producers must record boundaries when generating and assembling content, then
preserve them through chunking and persisted metadata. In particular, DOCX
image summaries are embedded both in body text and in table-cell HTML. A
marker on the final mixed text, string matching against a generated summary,
or treating a whole table as original is insufficient. Table asset provenance
must bind to the actual HTML, separately from the table chunk's placeholder
content. Source text that happens to equal a generated description stays source
text. Visual transcription is separate from both extracted source text and an
image description.

The design uses versioned, content-bound interval metadata in the existing JSON
metadata channel; it does not require rewriting legacy documents. Missing,
invalid, or stale provenance means `unknown`, never inferred `source`. Strict
consumers must reject unknown provenance or require explicit reindexing. Cache
shape versioning must prevent reuse of pre-protocol responses; cache eviction
alone cannot recover provenance missing from persisted chunks. HTTP and MCP
must preserve part-level provenance together with compound result attribution.
Image summarization and image retrieval remain enabled.

The implemented DOCX and standalone-image producers carry transient typed text
segments through list-based chunking. `extra_metadata.evidence_provenance`
binds chunk content; `extra_metadata.table_evidence_provenance` separately binds
the table HTML asset. Both use `version: evidence-provenance@v1`, UTF-8 SHA-256,
and contiguous `spans` with Python Unicode code-point `start`/`end` offsets and
`provenance`. Hydration validates the whole binding before splitting parts;
a missing binding, hash mismatch, gap, overlap or unknown version makes the
whole bound text `unknown`. Formatting and filename fallback are `system`.
Uninstrumented parser tracks also remain `unknown`, not inferred original text.
The scalar `evidence_text` is only a lossy convenience projection; consumers
requiring provenance must use `evidence` parts. Query cache keys include
`evidence_provenance_v1`; this does not upgrade existing indexed documents.

The Excel producer now binds the exact written HTML after serialization: cell
text is source, HTML scaffolding is system, and table summaries remain separate
metadata. Workbook cell text is HTML-escaped before rendering. Raw-text adapter
Markdown tables use the same exact-asset binding; generic Markdown/HTML/PDF
inputs keep unknown text authority. Markdown image context retains its input
authority, generated descriptions are inserted at the known asset reference as
generated-image-description, and deferred filename replacement rebinds intervals
without searching for summary text. Embedded-table image references and HTML
scaffolding remain system. Missing/invalid old metadata still decodes unknown.

These Excel and Markdown asset changes have deterministic parser-to-chunk JSON
and hydration coverage, not fresh live publication evidence. Earlier bounded
DOCX, PNG and plain-text live results do not certify these newly changed paths.
Phase 0.1 remains pending live verification and independent review.

Deterministic contracts cover DOCX assembly, long-body chunking, table-cell
image descriptions, standalone summary/transcription/fallback, JSON metadata
round-trip, fail-closed decoding and HTTP/MCP field preservation. These are not
a database publication or live deployment acceptance claim. Worker/API rollout,
real image-bearing reindex/retrieval and independent review remain pending.
