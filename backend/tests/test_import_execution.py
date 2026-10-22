from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.conf import LazySettings
from django.test import Client
from django.utils import timezone
from openpyxl import Workbook

from apps.data_imports.models import ImportJob, ImportJobStatus
from apps.data_imports.readers.xlsx import InvalidXlsxFileError
from apps.data_imports.services.import_execution import (
    EmptyWorkbookError,
    ImportAlreadyRunningError,
    ImportExecutionService,
)
from apps.data_imports.tasks import import_purchase_requests
from apps.procurement.models import PurchaseRequest, PurchaseRequestItem

pytestmark = pytest.mark.django_db(transaction=True)

HEADERS = (
    "date of request",
    "mark",
    "catalog number",
    "product name",
    "brand",
    "quantity",
    "price",
    "delivery time",
    "original / analogue",
)


def _source_file(root: Path) -> tuple[str, int]:
    storage_key = "job/requests.xlsx"
    path = root / storage_key
    path.parent.mkdir(parents=True)
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Requests"
    sheet.append(HEADERS)
    sheet.append(("2026-10-21", "ACME", "001", "Part 1", "Brand", 1, 10, 3, "original"))
    sheet.append((None, None, "002", "Part 2", "Brand", 2, 20, 4, "analogue"))
    sheet.append(("2026-10-22", None, "003", "Part 3", "Brand", 3, 30, 5, "original"))
    workbook.save(path)
    workbook.close()
    return storage_key, path.stat().st_size


def _job(root: Path) -> ImportJob:
    storage_key, size = _source_file(root)
    return ImportJob.objects.create(
        original_filename="requests.xlsx",
        storage_key=storage_key,
        source_byte_size=size,
        source_sha256="a" * 64,
        security_scope="local",
        configuration_snapshot={},
        configuration_hash="b" * 64,
        importer_version="1",
        selected_sheet_name="Requests",
    )


def test_execution_service_runs_complete_pipeline(
    tmp_path: Path,
    settings: LazySettings,
) -> None:
    settings.IMPORT_STORAGE_ROOT = str(tmp_path)
    settings.IMPORT_BATCH_SIZE = 2
    job = _job(tmp_path)

    ImportExecutionService().execute(job.id)

    job.refresh_from_db()
    assert job.status == ImportJobStatus.COMPLETED
    assert job.processed_row_count == 3
    assert job.imported_request_count == 2
    assert job.imported_item_count == 3
    assert job.checkpoint_row_number == 4
    assert PurchaseRequest.objects.filter(import_job=job).count() == 2
    assert PurchaseRequestItem.objects.filter(purchase_request__import_job=job).count() == 3


def test_execution_recovers_after_commit_before_worker_failure(
    tmp_path: Path,
    settings: LazySettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.IMPORT_STORAGE_ROOT = str(tmp_path)
    settings.IMPORT_BATCH_SIZE = 1
    job = _job(tmp_path)
    failing_service = ImportExecutionService()
    original_write_batch = failing_service._write_batch
    committed_once = False

    def commit_then_fail(*args: object, **kwargs: object) -> int:
        nonlocal committed_once
        checkpoint_row = original_write_batch(*args, **kwargs)
        if not committed_once:
            committed_once = True
            raise RuntimeError("simulated Worker failure")
        return checkpoint_row

    monkeypatch.setattr(failing_service, "_write_batch", commit_then_fail)
    with pytest.raises(RuntimeError, match="simulated Worker failure"):
        failing_service.execute(job.id)

    job.refresh_from_db()
    assert job.status == ImportJobStatus.FAILED
    assert job.checkpoint_row_number == 2

    ImportExecutionService().execute(job.id)

    job.refresh_from_db()
    assert job.status == ImportJobStatus.COMPLETED
    assert job.imported_request_count == 2
    assert job.imported_item_count == 3
    assert PurchaseRequestItem.objects.filter(purchase_request__import_job=job).count() == 3


def test_live_lease_prevents_second_worker(
    tmp_path: Path,
    settings: LazySettings,
) -> None:
    settings.IMPORT_STORAGE_ROOT = str(tmp_path)
    settings.IMPORT_LEASE_TIMEOUT_SECONDS = 300
    job = _job(tmp_path)
    job.status = ImportJobStatus.RUNNING
    active_token = uuid4()
    job.execution_token = active_token
    job.heartbeat_at = timezone.now()
    job.save(update_fields=("status", "execution_token", "heartbeat_at"))

    with pytest.raises(ImportAlreadyRunningError):
        ImportExecutionService().execute(job.id)

    job.refresh_from_db()
    assert job.status == ImportJobStatus.RUNNING
    assert job.execution_token == active_token
    assert job.attempt_count == 0
    assert job.failure_code == ""


def test_task_retries_live_lease_after_lease_timeout(
    settings: LazySettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.IMPORT_LEASE_TIMEOUT_SECONDS = 300
    execution_service = Mock()
    execution_service.execute.side_effect = ImportAlreadyRunningError("live lease")
    retry = Mock(side_effect=RuntimeError("retry scheduled"))
    monkeypatch.setattr(
        "apps.data_imports.tasks.ImportExecutionService",
        Mock(return_value=execution_service),
    )
    monkeypatch.setattr(import_purchase_requests, "retry", retry)

    with pytest.raises(RuntimeError, match="retry scheduled"):
        import_purchase_requests.run(str(uuid4()))

    retry.assert_called_once()
    assert retry.call_args.kwargs["countdown"] == 301
    assert isinstance(retry.call_args.kwargs["exc"], ImportAlreadyRunningError)
    assert import_purchase_requests.max_retries is None
    assert import_purchase_requests.retry_kwargs == {"max_retries": 5}


def test_post_claim_path_error_marks_job_failed(
    tmp_path: Path,
    settings: LazySettings,
) -> None:
    settings.IMPORT_STORAGE_ROOT = str(tmp_path)
    job = _job(tmp_path)
    job.storage_key = "../outside.xlsx"
    job.save(update_fields=("storage_key",))

    with pytest.raises(ValueError, match="escapes the configured storage root"):
        ImportExecutionService().execute(job.id)

    job.refresh_from_db()
    assert job.status == ImportJobStatus.FAILED
    assert job.execution_token is not None
    assert job.attempt_count == 1
    assert job.failure_code == "ValueError"
    assert str(tmp_path) not in job.failure_message


def test_invalid_xlsx_failure_is_safe_for_status_api(
    tmp_path: Path,
    settings: LazySettings,
    client: Client,
) -> None:
    settings.IMPORT_STORAGE_ROOT = str(tmp_path)
    job = _job(tmp_path)
    source_path = tmp_path / job.storage_key
    source_path.write_bytes(b"not an XLSX archive")

    with pytest.raises(InvalidXlsxFileError):
        ImportExecutionService().execute(job.id)

    job.refresh_from_db()
    assert job.status == ImportJobStatus.FAILED
    assert job.failure_message == "The uploaded file is not a valid or safe XLSX workbook."
    assert str(source_path) not in job.failure_message
    response = client.get(f"/api/v1/import-jobs/{job.id}/")
    assert response.status_code == 200
    assert response.json()["failure_message"] == job.failure_message
    assert str(source_path) not in response.content.decode()


def test_empty_workbook_has_controlled_domain_failure(
    tmp_path: Path,
    settings: LazySettings,
) -> None:
    settings.IMPORT_STORAGE_ROOT = str(tmp_path)
    job = _job(tmp_path)
    source_path = tmp_path / job.storage_key
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Requests"
    workbook.save(source_path)
    workbook.close()

    with pytest.raises(EmptyWorkbookError, match="does not contain a header row"):
        ImportExecutionService().execute(job.id)

    job.refresh_from_db()
    assert job.status == ImportJobStatus.FAILED
    assert job.failure_message == "The XLSX workbook does not contain a header row."


def test_stale_lease_can_be_taken_over(
    tmp_path: Path,
    settings: LazySettings,
) -> None:
    settings.IMPORT_STORAGE_ROOT = str(tmp_path)
    settings.IMPORT_LEASE_TIMEOUT_SECONDS = 60
    job = _job(tmp_path)
    stale_token = uuid4()
    job.status = ImportJobStatus.RUNNING
    job.execution_token = stale_token
    job.heartbeat_at = timezone.now() - timedelta(minutes=5)
    job.save(update_fields=("status", "execution_token", "heartbeat_at"))

    ImportExecutionService().execute(job.id)

    job.refresh_from_db()
    assert job.status == ImportJobStatus.COMPLETED
    assert job.execution_token != stale_token
