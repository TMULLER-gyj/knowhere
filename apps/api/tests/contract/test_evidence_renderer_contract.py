import asyncio
from copy import deepcopy
from typing import Any

import pytest

from app.api.v1.routes.retrieval import RetrievalQueryResponse

from app.mcp.retrieval_server import to_mcp_query_response
from shared.services.retrieval.execution.response_projection import (
    project_public_retrieval_response,
)
from shared.services.retrieval.execution.routes import _evidence_fields


def test_evidence_fields_flatten_composed_parts_in_result_order() -> None:
    rows: list[dict[str, Any]] = [
        {"composed": [{"type": "text", "text": "first"}]},
        {
            "composed": [
                {"type": "text", "text": "mid "},
                {"type": "image", "media_type": "image/png", "data": "abc"},
            ]
        },
        {"composed": [{"type": "text", "text": "<table><tr><td>metric</td></tr></table>"}]},
    ]

    for index, row in enumerate(rows):
        row.update(chunk_id=f"c{index}", document_id=f"d{index}")
    original = deepcopy(rows)
    fields = _evidence_fields(rows)

    assert fields["evidence"] == [
        {"type": "text", "text": "first", "chunk_id": "c0", "document_id": "d0"},
        {"type": "text", "text": "mid ", "chunk_id": "c1", "document_id": "d1"},
        {"type": "image", "media_type": "image/png", "data": "abc", "chunk_id": "c1", "document_id": "d1"},
        {"type": "text", "text": "<table><tr><td>metric</td></tr></table>", "chunk_id": "c2", "document_id": "d2"},
    ]
    assert rows == original
    assert fields["evidence_text"] == (
        "firstmid data:image/png;base64,abc<table><tr><td>metric</td></tr></table>"
    )


@pytest.mark.parametrize("router", ["small_corpus_all", "classic_topk", "agent_explore"])
def test_attribution_survives_http_and_mcp_projection(router: str) -> None:
    rows = [
        {"chunk_id": "shared", "document_id": document_id, "chunk_type": "text",
         "composed": [{"type": "text", "text": "<table>original</table>", "provenance": "generated-image-description"},
                      {"type": "image", "media_type": "image/png", "data": "abc", "provenance": "source"}]}
        for document_id in ["d1", "d2"]
    ]
    fields = _evidence_fields(rows)
    public = asyncio.run(project_public_retrieval_response({
        "namespace": "test", "query": "q", "router_used": router,
        "results": rows, **fields,
    }))
    http = RetrievalQueryResponse.model_validate(public).model_dump()
    mcp = to_mcp_query_response(http)
    for projection in [public, http, mcp]:
        assert projection["evidence"] == fields["evidence"]
        assert projection["evidence_text"] == fields["evidence_text"]
        assert [(part["chunk_id"], part["document_id"]) for part in projection["evidence"]] == [
            ("shared", "d1"), ("shared", "d1"), ("shared", "d2"), ("shared", "d2"),
        ]
        assert all("composed" not in row for row in projection["results"])


def test_cache_key_isolates_legacy_evidence_shapes() -> None:
    import hashlib

    from shared.services.retrieval.cache_service import _query_cache_key

    def current_key() -> str:
        return _query_cache_key(
            user_id="u", namespace="default", version=3, query="q",
            top_k=5, exclude_document_ids=[], exclude_sections=[],
        )
    # Frozen pre-provenance wire shape, not derived from the implementation.
    legacy_extra = "|".join([
        "", "", "delete", "", "[]", "False", "0.0", "None", "None",
        "", "", "", "",
    ])
    legacy_payload = f"q|5|||{legacy_extra}|document_scope_v1|None"
    key = current_key()
    for suffix in ["", "|evidence_attribution_v1"]:
        digest = hashlib.sha256((legacy_payload + suffix).encode()).hexdigest()
        assert key != f"retrieval:query:u:default:v3:{digest}"
    assert key == current_key()


@pytest.mark.parametrize("owners", [
    [("d1", "r1"), ("d2", "r1")],
    [("d1", "r1"), ("d1", "r2")],
])
def test_asset_composition_is_scoped_to_document_and_revision(monkeypatch, owners) -> None:
    from shared.services.retrieval.hydration import evidence_compose
    from shared.services.retrieval.hydration.result_assembly import assemble_retrieval_results

    monkeypatch.setattr(evidence_compose, "load_table_html", lambda row: row["content"])
    rows = []
    for index, (document_id, revision) in enumerate(owners):
        owner = {"document_id": document_id, "job_result_id": revision}
        rows.extend([
            {**owner, "chunk_id": "parent", "chunk_type": "text",
             "content": "[tables/a.html]", "chunk_metadata": {"connect_to": [
                 {"target": "asset", "relation": "embeds", "ref": "[tables/a.html]"},
             ]}},
            {**owner, "chunk_id": "asset", "chunk_type": "table",
             "content": f"<table>owner-{index}</table>"},
        ])
    result = asyncio.run(assemble_retrieval_results(
        rows=rows, exclude_document_ids=[], exclude_sections=[],
    ))
    assert len(result) == 2
    for index, row in enumerate(result):
        text = "".join(part.get("text", "") for part in row["composed"])
        assert f"owner-{index}" in text
        assert f"owner-{1 - index}" not in text
        assert (row["document_id"], row["job_result_id"]) == owners[index]


def test_standalone_excel_table_self_reference_remains_a_result(monkeypatch) -> None:
    from shared.services.chunks.evidence_provenance import marked, encode_provenance
    from shared.services.retrieval.hydration import evidence_compose
    from shared.services.retrieval.hydration.result_assembly import assemble_retrieval_results

    html = marked("<table>AuthoredSentinel</table>", "source")
    monkeypatch.setattr(evidence_compose, "load_table_html", lambda row: html)
    row = {
        "document_id": "d", "job_result_id": "r", "chunk_id": "table",
        "chunk_type": "table", "content": "tables/table-Metrics.html",
        "chunk_metadata": {
            "connect_to": [{"target": "table", "relation": "embeds",
                            "ref": "[tables/table-Metrics.html]"}],
            "table_evidence_provenance": encode_provenance(html),
        },
    }
    result = asyncio.run(assemble_retrieval_results(
        rows=[row], exclude_document_ids=[], exclude_sections=[],
    ))
    assert len(result) == 1
    assert result[0]["composed"] == [
        {"type": "text", "text": html, "provenance": "source"},
    ]
    assert _evidence_fields(result)["evidence"][0]["document_id"] == "d"


def test_missing_attribution_fails_instead_of_emitting_unowned_parts() -> None:
    with pytest.raises(KeyError, match="document_id"):
        _evidence_fields([{"chunk_id": "c", "composed": [{"type": "text", "text": "t"}]}])
    assert _evidence_fields([]) == {"evidence": [], "evidence_text": ""}


def test_mcp_query_response_keeps_evidence_and_debug_results() -> None:
    response = to_mcp_query_response(
        {
            "query": "q",
            "evidence": [{"type": "text", "text": "t"}],
            "evidence_text": "t",
            "results": [
                {
                    "content": "[images/a.png]",
                }
            ],
            "referenced_chunks": [{"chunk_id": "c1"}],
            "decision_trace": [{"step": 1}],
            "answer_text": "should drop",
        }
    )

    assert response == {
        "query": "q",
        "evidence": [{"type": "text", "text": "t"}],
        "evidence_text": "t",
        "results": [
            {
                "content": "[images/a.png]",
            }
        ],
        "referenced_chunks": [{"chunk_id": "c1"}],
        "decision_trace": [{"step": 1}],
    }


def test_public_results_keep_placeholders_without_composed() -> None:
    public = asyncio.run(
        project_public_retrieval_response(
            {
                "namespace": "default",
                "query": "q",
                "router_used": "classic",
                "evidence": [{"type": "text", "text": "<table>Q4</table>"}],
                "evidence_text": "<table>Q4</table>",
                "results": [
                    {
                        "chunk_id": "c1",
                        "chunk_type": "text",
                        "content": "见表 [tables/a.html]",
                        "composed": [{"type": "text", "text": "见表 <table>Q4</table>"}],
                        "score": 1,
                        "document_id": "d1",
                    }
                ],
            }
        )
    )

    assert public["evidence"] == [{"type": "text", "text": "<table>Q4</table>"}]
    assert public["results"][0]["content"] == "见表 [tables/a.html]"
    assert "composed" not in public["results"][0]
