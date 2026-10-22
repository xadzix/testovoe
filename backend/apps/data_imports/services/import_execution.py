import logging
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.data_imports.header_mapping import HeaderMapper, HeaderMappingError
from apps.data_imports.models import ImportJob, ImportJobStatus
from apps.data_imports.readers.xlsx import (
    InvalidXlsxFileError,
    WorksheetNotFoundError,
    XlsxSourceReader,
)
from apps.procurement.importing.converters import PurchaseRequestRowConverter
from apps.procurement.importing.grouping import PurchaseRequestRowGrouper
from apps.procurement.importing.schema import GroupingResult
from apps.procurement.importing.writer import (
    PurchaseRequestBatchWriter,
    StaleImportExecutionError,
)


class ImportAlreadyRunningError(RuntimeError):
    """Другой Worker владеет действующей арендой задания."""


class EmptyWorkbookError(ValueError):
    """Книга не содержит строки заголовков для импорта."""


logger = logging.getLogger(__name__)


class ImportExecutionService:
    """Последовательно выполнить импорт, восстанавливая состояние из checkpoint."""

    def __init__(self) -> None:
        self._writer = PurchaseRequestBatchWriter()

    def execute(self, import_job_id: UUID) -> None:
        claimed = self._claim(import_job_id)
        if claimed is None:
            return
        execution_token, storage_key, sheet_name = claimed

        try:
            source_root = Path(settings.IMPORT_STORAGE_ROOT).resolve()
            source_path = (source_root / storage_key).resolve()
            if source_root != source_path and source_root not in source_path.parents:
                raise ValueError("Import storage key escapes the configured storage root")
            checkpoint_row, checkpoint, restored_request = self._writer.load_checkpoint_context(
                import_job_id=import_job_id,
                execution_token=execution_token,
            )
            grouper = PurchaseRequestRowGrouper.from_checkpoint(checkpoint, restored_request)
            rows = XlsxSourceReader(source_path).iter_rows(sheet_name or None)
            header_row = next(rows, None)
            if header_row is None:
                raise EmptyWorkbookError("The XLSX workbook does not contain a header row")
            active_sheet_name = header_row.sheet_name
            converter = PurchaseRequestRowConverter(HeaderMapper().map(header_row))
            batch: list[GroupingResult] = []
            expected_checkpoint = checkpoint_row
            batch_size = int(settings.IMPORT_BATCH_SIZE)

            for source_row in rows:
                if source_row.sheet_name != active_sheet_name:
                    break
                if source_row.row_number <= checkpoint_row:
                    continue
                batch.append(grouper.process(converter.convert(source_row)))
                if len(batch) >= batch_size:
                    expected_checkpoint = self._write_batch(
                        import_job_id,
                        execution_token,
                        expected_checkpoint,
                        batch,
                        grouper,
                    )
                    batch.clear()

            if batch:
                self._write_batch(
                    import_job_id,
                    execution_token,
                    expected_checkpoint,
                    batch,
                    grouper,
                )
            self._complete(import_job_id, execution_token)
        except Exception as error:
            logger.exception("Import job %s failed", import_job_id)
            self._fail(import_job_id, execution_token, error)
            raise

    def _claim(self, import_job_id: UUID) -> tuple[UUID, str, str] | None:
        with transaction.atomic():
            import_job = ImportJob.objects.select_for_update().get(pk=import_job_id)
            if import_job.status in {
                ImportJobStatus.COMPLETED,
                ImportJobStatus.COMPLETED_WITH_ISSUES,
            }:
                return None
            lease_timeout = timedelta(seconds=int(settings.IMPORT_LEASE_TIMEOUT_SECONDS))
            if (
                import_job.status == ImportJobStatus.RUNNING
                and import_job.heartbeat_at is not None
                and import_job.heartbeat_at >= timezone.now() - lease_timeout
            ):
                raise ImportAlreadyRunningError("Import job has a live Worker lease")

            execution_token = uuid4()
            import_job.status = ImportJobStatus.RUNNING
            import_job.execution_token = execution_token
            import_job.attempt_count += 1
            import_job.heartbeat_at = timezone.now()
            import_job.started_at = import_job.started_at or timezone.now()
            import_job.finished_at = None
            import_job.failure_code = ""
            import_job.failure_message = ""
            import_job.save(
                update_fields=(
                    "status",
                    "execution_token",
                    "attempt_count",
                    "heartbeat_at",
                    "started_at",
                    "finished_at",
                    "failure_code",
                    "failure_message",
                    "updated_at",
                )
            )

        return execution_token, import_job.storage_key, import_job.selected_sheet_name

    def _write_batch(
        self,
        import_job_id: UUID,
        execution_token: UUID,
        expected_checkpoint: int,
        batch: list[GroupingResult],
        grouper: PurchaseRequestRowGrouper,
    ) -> int:
        self._writer.write_batch(
            import_job_id=import_job_id,
            execution_token=execution_token,
            expected_checkpoint_row_number=expected_checkpoint,
            results=batch,
            checkpoint=grouper.checkpoint(),
        )
        return batch[-1].source_row_number

    @staticmethod
    def _complete(import_job_id: UUID, execution_token: UUID) -> None:
        with transaction.atomic():
            import_job = ImportJob.objects.select_for_update().get(pk=import_job_id)
            if import_job.execution_token != execution_token:
                raise StaleImportExecutionError("Import ownership changed before completion")
            import_job.status = (
                ImportJobStatus.COMPLETED_WITH_ISSUES
                if import_job.invalid_row_count
                else ImportJobStatus.COMPLETED
            )
            import_job.finished_at = timezone.now()
            import_job.save(update_fields=("status", "finished_at", "updated_at"))

    @staticmethod
    def _fail(import_job_id: UUID, execution_token: UUID, error: Exception) -> None:
        with transaction.atomic():
            import_job = ImportJob.objects.select_for_update().get(pk=import_job_id)
            if import_job.execution_token != execution_token:
                return
            import_job.status = ImportJobStatus.FAILED
            import_job.finished_at = timezone.now()
            import_job.failure_code = type(error).__name__[:100]
            import_job.failure_message = _safe_failure_message(error)
            import_job.save(
                update_fields=(
                    "status",
                    "finished_at",
                    "failure_code",
                    "failure_message",
                    "updated_at",
                )
            )


def _safe_failure_message(error: Exception) -> str:
    if isinstance(error, InvalidXlsxFileError):
        return "The uploaded file is not a valid or safe XLSX workbook."
    if isinstance(error, WorksheetNotFoundError):
        return "The selected worksheet does not exist."
    if isinstance(error, HeaderMappingError):
        return "The XLSX header row is invalid."
    if isinstance(error, EmptyWorkbookError):
        return "The XLSX workbook does not contain a header row."
    return "The import failed because of an internal error. See Worker logs for details."
