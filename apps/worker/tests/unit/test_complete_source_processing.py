from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_complete_export_uses_existing_job_finalizer_without_retrieval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.services.complete_source import processing
    from app.services.document_ingestion.source_preparation import PreparedSourceFile

    source = tmp_path / "source.txt"
    source.write_bytes(b"exact  source\nTAIL")
    context = SimpleNamespace(job_metadata={"complete_source": {"schema_version": "knowhere-complete-source-request@v1", "kind": "text"}}, job_user_id="offline-user")
    lifecycle = SimpleNamespace(update_progress=Mock(), finalize_job_success=Mock(return_value={"status": "success"}))
    storage = SimpleNamespace(upload=Mock(return_value=SimpleNamespace(zip_key="results/job_offline.zip")))
    monkeypatch.setattr(processing, "charge_parse_job_pages", Mock(return_value=object()))
    monkeypatch.setattr(processing, "record_processing_start", Mock())
    monkeypatch.setattr(processing, "persist_job_metadata_updates", Mock())
    result = processing.process_complete_source(
        job_id="job_offline", job_context=context,
        source=PreparedSourceFile("source.txt", "source.txt", str(source), ".txt"),
        output_dir=str(tmp_path), lifecycle_service=lifecycle, result_storage=storage,
    )
    assert result == {"status": "success"}
    arguments = lifecycle.finalize_job_success.call_args.kwargs
    assert arguments["publish_to_retrieval"] is False
    assert arguments["chunks"] == []
    assert arguments["result_s3_key"] == "results/job_offline.zip"
    assert len(arguments["checksum"]) == 64
    assert storage.upload.call_args.kwargs["artifact_refs"] == set()


def test_partial_export_fails_before_billing_upload_or_finalization(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.services.complete_source import processing
    from app.services.document_ingestion.source_preparation import PreparedSourceFile
    from shared.core.exceptions.domain_exceptions import ValidationException

    source = tmp_path / "source.html"
    source.write_bytes(b'<p>body</p><img src="file:///private/secret">')
    context = SimpleNamespace(job_metadata={"complete_source": {"schema_version": "knowhere-complete-source-request@v1", "kind": "document"}}, job_user_id="offline-user")
    bill = Mock()
    monkeypatch.setattr(processing, "charge_parse_job_pages", bill)
    lifecycle = SimpleNamespace(update_progress=Mock(), finalize_job_success=Mock())
    storage = SimpleNamespace(upload=Mock())
    with pytest.raises(ValidationException) as error:
        processing.process_complete_source(
            job_id="job_offline", job_context=context,
            source=PreparedSourceFile("source.html", "source.html", str(source), ".html"),
            output_dir=str(tmp_path), lifecycle_service=lifecycle, result_storage=storage,
        )
    assert error.value.user_message == "complete-source-external-resource-required"
    bill.assert_not_called()
    storage.upload.assert_not_called()
    lifecycle.finalize_job_success.assert_not_called()


def test_finalizer_skip_retrieval_still_commits_job_result_and_terminal_state() -> None:
    from shared.services.jobs.lifecycle.success_finalizer import SyncJobSuccessFinalizer

    writer = SimpleNamespace(upsert_job_result=Mock(return_value=SimpleNamespace(id="result")), replace_chunks=Mock())
    publication = SimpleNamespace(publish_result=Mock())
    state = SimpleNamespace(mark_completed_outcome=Mock(return_value=SimpleNamespace(succeeded=True)))
    outbox = SimpleNamespace(create_event=Mock(return_value=None))
    finalizer = SyncJobSuccessFinalizer(state_machine=state, result_writer=writer, publication_finalizer=publication, webhook_outbox=outbox)
    result = finalizer.finalize(
        object(), job_id="job_offline", result_s3_key="results/job_offline.zip",
        checksum="a" * 64, zip_size=10, chunks=[], stored_count=0,
        delivery_mode="url", section_summaries=None, publish_to_retrieval=False,
    )
    assert result.response.status == "success"
    publication.publish_result.assert_not_called()
    state.mark_completed_outcome.assert_called_once()
    writer.upsert_job_result.assert_called_once()


def test_source_preparation_keeps_exact_bytes_without_office_normalizer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.services.document_ingestion import source_preparation

    path = tmp_path / "source.docx"
    path.write_bytes(b"exact uploaded bytes")
    storage = SimpleNamespace(verify_upload_exists=Mock(return_value={"exists": True, "size": path.stat().st_size}), download_upload_to_temp=Mock(return_value=str(path)))
    monkeypatch.setattr(source_preparation, "JobFileStorage", lambda: storage)
    normalize = Mock(side_effect=AssertionError("complete mode must not normalize before hashing"))
    monkeypatch.setattr(source_preparation, "normalize_office_source", normalize)
    monkeypatch.setattr(source_preparation, "prepare_internal_parse_input", lambda *args, **kwargs: SimpleNamespace(file_path=str(path), internal_filename="source.docx"))
    result = source_preparation.prepare_source_file(
        job_id="job_offline", job_context=SimpleNamespace(job_metadata={"complete_source": {}, "source_file_name": "source.docx"}, s3_key="uploads/job_offline.docx"), input_dir=str(tmp_path),
    )
    assert result.local_file_path == str(path)
    normalize.assert_not_called()


def test_complete_mode_dispatch_never_calls_legacy_parser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.services.complete_source import processing
    from app.services.document_ingestion import processing_run

    context = SimpleNamespace(job_metadata={"complete_source": {"schema_version": "knowhere-complete-source-request@v1", "kind": "text"}})
    lifecycle = SimpleNamespace(update_progress=Mock())
    workspace = SimpleNamespace(input_dir=str(tmp_path), output_dir=str(tmp_path))
    complete = Mock(return_value={"status": "success"})
    monkeypatch.setattr(processing, "process_complete_source", complete)
    monkeypatch.setattr(processing_run, "prepare_source_file", Mock(return_value=object()))
    monkeypatch.setattr(processing_run, "get_result_storage", Mock(return_value=object()))
    monkeypatch.setattr(processing_run, "persist_job_metadata_updates", Mock())
    legacy = Mock(side_effect=AssertionError("legacy parser must never run"))
    monkeypatch.setattr(processing_run, "execute_document_parse", legacy)
    result = processing_run._run_parse_job(
        job_id="job_offline", job_context=context, lifecycle_service=lifecycle,
        task_workspace=workspace,
    )
    assert result == {"status": "success"}
    complete.assert_called_once()
    legacy.assert_not_called()


def test_encoding_failure_has_closed_safe_job_error_fields(tmp_path: Path) -> None:
    from app.services.complete_source import processing
    from app.services.document_ingestion.source_preparation import PreparedSourceFile
    from shared.core.exceptions.domain_exceptions import ValidationException

    path = tmp_path / "source.txt"
    path.write_bytes(b"\xff\xff")
    context = SimpleNamespace(job_metadata={"complete_source": {"schema_version": "knowhere-complete-source-request@v1", "kind": "text"}})
    with pytest.raises(ValidationException) as raised:
        processing.process_complete_source(
            job_id="job_encoding", job_context=context,
            source=PreparedSourceFile("source.txt", "source.txt", str(path), ".txt"),
            output_dir=str(tmp_path), lifecycle_service=Mock(), result_storage=Mock(),
        )
    assert raised.value.to_client("job_encoding") == {
        "success": False,
        "error": {
            "code": "INVALID_ARGUMENT", "message": "complete-source-encoding-required",
            "request_id": "job_encoding",
            "details": {"violations": [{"field": "complete_source", "description": "complete-source-encoding-required"}]},
        },
    }
