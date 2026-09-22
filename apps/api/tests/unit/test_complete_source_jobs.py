from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from shared.core.exceptions.domain_exceptions import PermissionDeniedException, ValidationException
from shared.models.schemas.complete_source import COMPLETE_SOURCE_EXTENSIONS
from shared.models.schemas.job import JobCreate, JobCreateV2
from shared.models.schemas.job_metadata import JobMetadataHelper


def request(kind: str = "document", name: str = "attempt.pdf", **extra):
    return JobCreateV2.model_validate({
        "source_type": "file", "file_name": name,
        "complete_source": {"schema_version": "knowhere-complete-source-request@v1", "kind": kind},
        **extra,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,extension", [(kind, extension) for kind, extensions in COMPLETE_SOURCE_EXTENSIONS.items() for extension in sorted(extensions)])
async def test_complete_export_admits_required_formats(kind: str, extension: str) -> None:
    from app.services.document_ingestion.service import DocumentIngestionService

    service = object.__new__(DocumentIngestionService)
    assert await service._validate_create_payload(request(kind, "attempt" + extension), api_version="v2") == extension


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"source_type": "url", "source_url": "https://example.invalid/a.pdf"},
    {"document_id": "some-existing-document"},
    {"llm_config": {"api_key": "synthetic-test-key", "model": "test", "base_url": "https://example.invalid/v1"}},
    {"file_name": "attempt.exe"},
])
async def test_complete_export_rejects_updates_urls_llm_and_wrong_format(extra: dict) -> None:
    from app.services.document_ingestion.service import DocumentIngestionService

    service = object.__new__(DocumentIngestionService)
    with pytest.raises(ValidationException):
        await service._validate_create_payload(request(**extra), api_version="v2")


@pytest.mark.asyncio
async def test_v1_does_not_silently_accept_complete_export() -> None:
    from app.services.document_ingestion.service import DocumentIngestionService

    payload = JobCreate.model_validate(request().model_dump())
    with pytest.raises(ValidationException):
        await object.__new__(DocumentIngestionService)._validate_create_payload(payload, api_version="v1")


def test_closed_request_and_metadata_survive_worker_handoff() -> None:
    payload = request("code", "attempt.py")
    metadata = JobMetadataHelper.create_from_request(payload, api_version="v2")
    assert metadata["complete_source"] == {"schema_version": "knowhere-complete-source-request@v1", "kind": "code"}
    with pytest.raises(ValidationError):
        request(complete_source={"schema_version": "knowhere-complete-source-request@v1", "kind": "code", "url": "forbidden"})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["waiting-file", "running", "done", "failed"])
async def test_upload_instructions_refreshed_only_for_waiting_complete_job(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    from app.services.jobs import result_projection

    created = datetime.now(timezone.utc)
    job = SimpleNamespace(
        job_id="job_fixed", user_id="user-a", status=status, source_type="file",
        created_at=created, updated_at=created, job_result=None,
        error_message=None, billing_status="pending",
    )
    metadata = JobMetadataHelper.create_from_request(request(), api_version="v2")
    metadata["document_id"] = "not-a-published-document"
    upload = AsyncMock(return_value={"upload_url": "https://storage.example.invalid/upload", "upload_headers": {"Content-Type": "application/pdf"}, "expires_in": 60})
    monkeypatch.setattr(result_projection, "FileUploadService", lambda: SimpleNamespace(generate_upload_url=upload))
    monkeypatch.setattr(result_projection, "_resolve_result_delivery", AsyncMock(return_value=(None, None, created)))
    first = await result_projection.build_job_result_response(job=job, job_metadata=metadata, progress=None)
    second = await result_projection.build_job_result_response(job=job, job_metadata=metadata, progress=None)
    assert first.document_id is None and second.document_id is None
    if status == "waiting-file":
        assert upload.await_count == 2
        upload.assert_awaited_with("job_fixed", ".pdf")
        assert first.upload_headers == {"Content-Type": "application/pdf"}
        assert first.expires_in == 60
    else:
        upload.assert_not_called()
        assert first.upload_url is None


@pytest.mark.asyncio
async def test_cross_user_job_rejected_before_upload_instruction_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.jobs import job_read_model

    repository = SimpleNamespace(get_job_by_id=AsyncMock(return_value=SimpleNamespace(user_id="owner-a")))
    projection = AsyncMock()
    monkeypatch.setattr(job_read_model, "JobRepository", lambda: repository)
    monkeypatch.setattr(job_read_model, "build_job_result_response", projection)
    with pytest.raises(PermissionDeniedException):
        await job_read_model.get_job_result_for_user(AsyncMock(), job_id="job_owned", user_id="owner-b")
    projection.assert_not_called()


def test_persisted_complete_source_failure_projects_exact_safe_fields() -> None:
    from app.services.jobs.result_projection import build_error_response

    response = build_error_response(
        SimpleNamespace(job_id="job_encoding", error_code="INVALID_ARGUMENT", error_message="complete-source-encoding-required"),
        {"error_details": {"violations": [{"field": "complete_source", "description": "complete-source-encoding-required"}]}},
    )
    assert response is not None
    assert response.model_dump() == {
        "code": "INVALID_ARGUMENT", "message": "complete-source-encoding-required",
        "request_id": "job_encoding",
        "details": {"violations": [{"field": "complete_source", "description": "complete-source-encoding-required"}]},
    }
