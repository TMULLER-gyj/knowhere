from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.models.schemas.job import JobCreateV2
from shared.models.schemas.job_metadata import JobMetadataHelper


def ordinary_request(name: str = "source-attempt.PDF") -> JobCreateV2:
    return JobCreateV2.model_validate({
        "source_type": "file", "file_name": name,
        "parsing_params": {"summary_txt": False, "summary_table": False},
    })


def job(status: str, *, source_type: str = "file", s3_key: str | None = "uploads/job_fixed.PDF"):
    created = datetime.now(timezone.utc)
    return SimpleNamespace(
        job_id="job_fixed", user_id="user-a", status=status, source_type=source_type, s3_key=s3_key,
        created_at=created, updated_at=created, job_result=None,
        error_message=None, billing_status="pending",
    )


async def project(monkeypatch: pytest.MonkeyPatch, target, metadata):
    from app.services.jobs import result_projection

    upload = AsyncMock(return_value={
        "upload_url": "https://storage.example.invalid/upload",
        "upload_headers": {"Content-Type": "application/octet-stream"}, "expires_in": 60,
    })
    monkeypatch.setattr(result_projection, "FileUploadService", lambda: SimpleNamespace(generate_upload_url=upload))
    monkeypatch.setattr(result_projection, "_resolve_result_delivery", AsyncMock(return_value=(None, None, target.created_at)))
    response = await result_projection.build_job_result_response(job=target, job_metadata=metadata, progress=None)
    return response, upload


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["waiting-file", "pending", "running", "done", "failed"])
async def test_ordinary_file_job_refreshes_upload_instructions_only_while_waiting(
    monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    metadata = JobMetadataHelper.create_from_request(ordinary_request(), api_version="v2")
    metadata["document_id"] = "doc_indexed"
    response, upload = await project(monkeypatch, job(status), metadata)
    # An ordinary Job keeps its retrieval document identity, unlike a complete export.
    assert response.document_id == "doc_indexed"
    if status == "waiting-file":
        # The stored creation key keeps the original extension case.
        upload.assert_awaited_once_with("job_fixed", ".PDF")
        assert response.upload_url == "https://storage.example.invalid/upload"
        assert response.upload_headers == {"Content-Type": "application/octet-stream"}
        assert response.expires_in == 60
    else:
        upload.assert_not_called()
        assert response.upload_url is None and response.upload_headers is None


@pytest.mark.asyncio
async def test_upload_instructions_fall_back_to_submitted_name_without_stored_key(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = JobMetadataHelper.create_from_request(ordinary_request("source-attempt.Docx"), api_version="v2")
    _, upload = await project(monkeypatch, job("waiting-file", s3_key=None), metadata)
    upload.assert_awaited_once_with("job_fixed", ".Docx")


@pytest.mark.asyncio
async def test_url_job_never_receives_upload_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = JobMetadataHelper.create_from_request(ordinary_request(), api_version="v2")
    response, upload = await project(monkeypatch, job("waiting-file", source_type="url", s3_key=None), metadata)
    upload.assert_not_called()
    assert response.upload_url is None
