"""Complete export inside the existing Job state/billing/storage transaction path."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.document_ingestion.page_estimator import PageEstimator
from app.services.document_ingestion.processing_billing import charge_parse_job_pages, record_processing_start
from app.services.document_ingestion.processing_context import ParseJobContext, persist_job_metadata_updates
from app.services.document_ingestion.source_preparation import PreparedSourceFile
from shared.core.exceptions.domain_exceptions import ValidationException
from shared.models.schemas.complete_source import CompleteSourceRequest
from shared.services.storage.result_storage import ResultStorage

from .export import CompleteSourceError, build_complete_source


def process_complete_source(
    *, job_id: str, job_context: ParseJobContext, source: PreparedSourceFile,
    output_dir: str, lifecycle_service: Any, result_storage: ResultStorage,
) -> dict[str, object]:
    started = datetime.now(timezone.utc)
    try:
        request = CompleteSourceRequest.model_validate(job_context.job_metadata["complete_source"])
        zip_path = Path(output_dir) / "complete-source.zip"
        manifest, checksum, size = build_complete_source(
            job_id=job_id, source_path=Path(source.local_file_path),
            request=request, output_path=zip_path,
        )
    except CompleteSourceError as exc:
        raise ValidationException(
            user_message=exc.code,
            violations=[{"field": "complete_source", "description": exc.code}],
        ) from exc
    # Only run the established billing estimator after bounded format validation.
    workload = PageEstimator.estimate_workload(source.local_file_path)
    billing = charge_parse_job_pages(
        job_id=job_id, filename=source.source_file_name,
        job_user_id=job_context.job_user_id, workload_estimate=workload,
    )
    record_processing_start(
        job_id=job_id, job_context=job_context, billing_snapshot=billing,
        processing_started_at=started, workload_estimate=workload,
    )
    lifecycle_service.update_progress(job_id, progress=90, message="Uploading complete source export...")
    uploaded = result_storage.upload(
        job_id=job_id, result_dir=output_dir, zip_file_path=str(zip_path), artifact_refs=set(),
    )
    persist_job_metadata_updates(
        job_id=job_id, job_context=job_context,
        metadata_updates={
            "complete_source_schema": manifest.schema_version,
            "complete_source_coverage": manifest.coverage.model_dump(),
            "processing_completed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    result = lifecycle_service.finalize_job_success(
        job_id=job_id, result_s3_key=uploaded.zip_key, checksum=checksum,
        zip_size=size, chunks=[], stored_count=0, delivery_mode="url",
        publish_to_retrieval=False,
    )
    if result.get("status") == "success":
        lifecycle_service.update_progress(job_id, progress=100, message="Complete source export ready")
    return dict(result)
