import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.conf import LazySettings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.data_imports.models import (
    ImportIssueSeverity,
    ImportJob,
    ImportJobStatus,
    ImportRowIssue,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _job() -> ImportJob:
    return ImportJob.objects.create(
        original_filename="requests.xlsx",
        storage_key="job/requests.xlsx",
        source_byte_size=10,
        source_sha256="a" * 64,
        security_scope="local",
        configuration_snapshot={},
        configuration_hash="b" * 64,
        importer_version="1",
    )


def test_post_upload_streams_file_and_publishes_job_after_commit(
    tmp_path: Path,
    settings: LazySettings,
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.IMPORT_STORAGE_ROOT = str(tmp_path)
    delay = Mock()
    monkeypatch.setattr(
        "apps.data_imports.services.import_jobs.import_purchase_requests.delay",
        delay,
    )
    content = b"synthetic xlsx bytes"

    response = client.post(
        "/api/v1/import-jobs/",
        {"file": SimpleUploadedFile("requests.xlsx", content), "sheet_name": "Requests"},
    )

    assert response.status_code == 202
    payload = response.json()
    job = ImportJob.objects.get(pk=payload["id"])
    assert (tmp_path / job.storage_key).read_bytes() == content
    assert job.source_sha256 == hashlib.sha256(content).hexdigest()
    assert job.selected_sheet_name == "Requests"
    delay.assert_called_once_with(str(job.id))


def test_post_marks_job_failed_when_task_publication_fails(
    tmp_path: Path,
    settings: LazySettings,
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.IMPORT_STORAGE_ROOT = str(tmp_path)
    delay = Mock(side_effect=ConnectionError("redis internal details"))
    monkeypatch.setattr(
        "apps.data_imports.services.import_jobs.import_purchase_requests.delay",
        delay,
    )

    response = client.post(
        "/api/v1/import-jobs/",
        {"file": SimpleUploadedFile("requests.xlsx", b"synthetic xlsx bytes")},
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": "import_service_unavailable",
        "message": "The import service is temporarily unavailable",
    }
    import_job = ImportJob.objects.get()
    assert import_job.status == ImportJobStatus.FAILED
    assert import_job.failure_code == "TaskPublicationError"
    assert import_job.failure_message == "The import could not be queued. Try again later."
    assert "redis internal details" not in response.content.decode()
    delay.assert_called_once_with(str(import_job.id))


def test_task_publication_retry_is_bounded(settings: LazySettings) -> None:
    assert settings.CELERY_TASK_PUBLISH_RETRY is True
    assert settings.CELERY_TASK_PUBLISH_RETRY_POLICY["max_retries"] == 3


def test_post_rejects_missing_or_non_xlsx_file(client: Client) -> None:
    missing = client.post("/api/v1/import-jobs/", {})
    wrong_type = client.post(
        "/api/v1/import-jobs/",
        {"file": SimpleUploadedFile("requests.csv", b"a,b")},
    )

    assert missing.status_code == 400
    assert wrong_type.status_code == 400


def test_post_rejects_oversized_upload_and_removes_partial_file(
    tmp_path: Path,
    settings: LazySettings,
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.IMPORT_STORAGE_ROOT = str(tmp_path)
    settings.IMPORT_MAX_FILE_SIZE_BYTES = 5
    delay = Mock()
    monkeypatch.setattr(
        "apps.data_imports.services.import_jobs.import_purchase_requests.delay",
        delay,
    )

    response = client.post(
        "/api/v1/import-jobs/",
        {"file": SimpleUploadedFile("requests.xlsx", b"123456")},
    )

    assert response.status_code == 413
    assert response.json()["error"] == "upload_too_large"
    assert ImportJob.objects.count() == 0
    assert list(tmp_path.iterdir()) == []
    delay.assert_not_called()


@pytest.mark.parametrize(
    ("filename", "sheet_name"),
    [
        (f"{'я' * 126}.xlsx", ""),
        ("requests.xlsx", "S" * 32),
    ],
)
def test_post_validates_filename_and_sheet_name_before_creating_files(
    filename: str,
    sheet_name: str,
    tmp_path: Path,
    settings: LazySettings,
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.IMPORT_STORAGE_ROOT = str(tmp_path)
    delay = Mock()
    monkeypatch.setattr(
        "apps.data_imports.services.import_jobs.import_purchase_requests.delay",
        delay,
    )

    response = client.post(
        "/api/v1/import-jobs/",
        {"file": SimpleUploadedFile(filename, b"content"), "sheet_name": sheet_name},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_upload"
    assert ImportJob.objects.count() == 0
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []
    delay.assert_not_called()


def test_get_status_returns_import_counters(client: Client) -> None:
    job = _job()
    job.processed_row_count = 12
    job.imported_item_count = 10
    job.save(update_fields=("processed_row_count", "imported_item_count"))

    response = client.get(f"/api/v1/import-jobs/{job.id}/")

    assert response.status_code == 200
    assert response.json()["processed_row_count"] == 12
    assert response.json()["imported_item_count"] == 10


def test_get_issues_is_paginated(client: Client) -> None:
    job = _job()
    for row_number in range(2, 5):
        ImportRowIssue.objects.create(
            import_job=job,
            source_sheet_name="Requests",
            source_row_number=row_number,
            severity=ImportIssueSeverity.ERROR,
            issue_codes=["invalid_quantity"],
            messages=["invalid quantity"],
            raw_row_snapshot={"quantity": "bad"},
        )

    response = client.get(f"/api/v1/import-jobs/{job.id}/row-issues/?page=2&page_size=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 3
    assert payload["page"] == 2
    assert [issue["source_row_number"] for issue in payload["results"]] == [4]
