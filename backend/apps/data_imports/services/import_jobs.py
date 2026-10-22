import hashlib
import json
import logging
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

from apps.data_imports.models import ImportJob, ImportJobStatus
from apps.data_imports.tasks import import_purchase_requests

logger = logging.getLogger(__name__)


class InvalidUploadError(ValueError):
    """Загруженный файл не соответствует контракту XLSX endpoint."""


class UploadTooLargeError(InvalidUploadError):
    """Загруженный файл превышает настроенный предел размера."""


class ImportPublicationError(RuntimeError):
    """Celery не подтвердил публикацию задачи после ограниченных retry."""


class ImportJobService:
    """Сохранить XLSX и опубликовать задание только после commit транзакции."""

    def create(self, uploaded_file: UploadedFile, *, sheet_name: str = "") -> ImportJob:
        original_filename = Path(uploaded_file.name or "").name
        if not original_filename or Path(original_filename).suffix.casefold() != ".xlsx":
            raise InvalidUploadError("An .xlsx file is required")
        if len(original_filename) > 255 or len(original_filename.encode("utf-8")) > 255:
            raise InvalidUploadError("The XLSX filename is too long")
        if len(sheet_name) > 31:
            raise InvalidUploadError("The XLSX sheet name is too long")

        job_id = uuid4()
        storage_key = f"{job_id}/{original_filename}"
        storage_root = Path(settings.IMPORT_STORAGE_ROOT)
        source_path = storage_root / storage_key
        source_path.parent.mkdir(parents=True, exist_ok=False)

        source_hash = hashlib.sha256()
        source_size = 0
        max_file_size = int(settings.IMPORT_MAX_FILE_SIZE_BYTES)
        try:
            with source_path.open("wb") as destination:
                for chunk in uploaded_file.chunks():
                    source_size += len(chunk)
                    if source_size > max_file_size:
                        raise UploadTooLargeError(
                            f"The XLSX file exceeds the {max_file_size}-byte limit"
                        )
                    destination.write(chunk)
                    source_hash.update(chunk)
        except Exception:
            source_path.unlink(missing_ok=True)
            source_path.parent.rmdir()
            raise

        configuration = {"sheet_name": sheet_name, "header_row_number": 1}
        configuration_json = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        publication_error: Exception | None = None

        def publish_import_task() -> None:
            nonlocal publication_error
            try:
                import_purchase_requests.delay(str(job_id))
            except Exception as error:
                publication_error = error
                logger.exception("Could not publish Celery task for import job %s", job_id)

        with transaction.atomic():
            import_job = ImportJob.objects.create(
                id=job_id,
                original_filename=original_filename,
                storage_key=storage_key,
                source_byte_size=source_size,
                source_sha256=source_hash.hexdigest(),
                security_scope="local",
                configuration_snapshot=configuration,
                configuration_hash=hashlib.sha256(configuration_json.encode()).hexdigest(),
                importer_version="1",
                selected_sheet_name=sheet_name,
            )
            transaction.on_commit(publish_import_task)

        if publication_error is not None:
            failure_time = timezone.now()
            ImportJob.objects.filter(pk=job_id, status=ImportJobStatus.QUEUED).update(
                status=ImportJobStatus.FAILED,
                finished_at=failure_time,
                failure_code="TaskPublicationError",
                failure_message="The import could not be queued. Try again later.",
                updated_at=failure_time,
            )
            raise ImportPublicationError("The import service is temporarily unavailable") from (
                publication_error
            )
        return import_job
