import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.data_imports.models import ImportIssueSeverity, ImportJob, ImportJobStatus, ImportRowIssue
from apps.procurement.importing.converters import normalize_customer_comparison_key
from apps.procurement.importing.schema import (
    GrouperCheckpoint,
    GroupingDecision,
    GroupingResult,
    GroupState,
    PurchaseRequestCandidate,
)
from apps.procurement.models import PurchaseRequest, PurchaseRequestItem


class StaleImportExecutionError(RuntimeError):
    """Worker больше не владеет заданием импорта."""


class BatchCheckpointConflict(RuntimeError):
    """Пакет не продолжает текущий зафиксированный checkpoint."""


class StoredCheckpointInvariantError(RuntimeError):
    """Сохранённый checkpoint не позволяет восстановить контекст заявки."""


class PurchaseRequestBatchWriter:
    """Атомарно сохранить ограниченный пакет и checkpoint."""

    def write_batch(
        self,
        *,
        import_job_id: UUID,
        execution_token: UUID,
        expected_checkpoint_row_number: int,
        results: Sequence[GroupingResult],
        checkpoint: GrouperCheckpoint,
    ) -> bool:
        first_row, last_row = _validate_batch(results, checkpoint)
        batch_fingerprint = _batch_fingerprint(results, checkpoint)
        checkpoint_state = checkpoint.to_dict()
        checkpoint_state["batch"] = {
            "first_row_number": first_row,
            "last_row_number": last_row,
            "fingerprint": batch_fingerprint,
        }

        with transaction.atomic():
            import_job = ImportJob.objects.select_for_update().get(pk=import_job_id)
            self._verify_owner(import_job, execution_token)
            if import_job.checkpoint_row_number == last_row:
                if import_job.checkpoint_state == checkpoint_state:
                    return False
                raise BatchCheckpointConflict("Committed batch identity does not match retry")
            if import_job.checkpoint_row_number != expected_checkpoint_row_number:
                raise BatchCheckpointConflict(
                    "Import checkpoint changed before this batch acquired the lock"
                )
            if (
                first_row <= expected_checkpoint_row_number
                or last_row <= expected_checkpoint_row_number
            ):
                raise BatchCheckpointConflict("Batch does not advance the checkpoint")

            request_by_origin, created_request_count = self._persist_requests(
                import_job,
                results,
            )
            item_count = self._persist_items(results, request_by_origin)
            issue_count = self._persist_issues(import_job, results)

            import_job.processed_row_count += len(results)
            import_job.skipped_row_count += sum(
                result.decision is GroupingDecision.SKIP for result in results
            )
            import_job.invalid_row_count += issue_count
            import_job.imported_request_count += created_request_count
            import_job.imported_item_count += item_count
            import_job.checkpoint_row_number = last_row
            import_job.checkpoint_state = checkpoint_state
            import_job.heartbeat_at = timezone.now()
            import_job.save(
                update_fields=(
                    "processed_row_count",
                    "skipped_row_count",
                    "invalid_row_count",
                    "imported_request_count",
                    "imported_item_count",
                    "checkpoint_row_number",
                    "checkpoint_state",
                    "heartbeat_at",
                    "updated_at",
                )
            )
        return True

    def load_checkpoint_context(
        self,
        *,
        import_job_id: UUID,
        execution_token: UUID,
    ) -> tuple[int, GrouperCheckpoint, PurchaseRequestCandidate | None]:
        import_job = ImportJob.objects.get(pk=import_job_id)
        self._verify_owner(import_job, execution_token)
        if not import_job.checkpoint_state:
            checkpoint = GrouperCheckpoint(
                version=1,
                sheet_name=import_job.selected_sheet_name,
                group_state=GroupState.NO_ACTIVE_REQUEST,
            )
            return import_job.checkpoint_row_number, checkpoint, None

        checkpoint = GrouperCheckpoint.from_dict(import_job.checkpoint_state)
        if checkpoint.group_state is not GroupState.ACTIVE_VALID_REQUEST:
            return import_job.checkpoint_row_number, checkpoint, None
        if checkpoint.source_start_row_number is None:
            raise StoredCheckpointInvariantError("Valid checkpoint has no request origin")

        try:
            stored_request = PurchaseRequest.objects.get(
                import_job=import_job,
                source_sheet_name=checkpoint.sheet_name,
                source_start_row_number=checkpoint.source_start_row_number,
            )
        except PurchaseRequest.DoesNotExist as error:
            raise StoredCheckpointInvariantError(
                "Checkpoint references a missing purchase request"
            ) from error

        candidate = PurchaseRequestCandidate(
            source_sheet_name=stored_request.source_sheet_name,
            source_start_row_number=stored_request.source_start_row_number,
            request_date=stored_request.request_date,
            request_date_raw=stored_request.request_date_raw,
            customer_name=stored_request.customer_name,
            customer_name_raw=stored_request.customer_name_raw,
            customer_comparison_key=normalize_customer_comparison_key(stored_request.customer_name),
        )
        return import_job.checkpoint_row_number, checkpoint, candidate

    @staticmethod
    def _verify_owner(import_job: ImportJob, execution_token: UUID) -> None:
        if import_job.execution_token != execution_token:
            raise StaleImportExecutionError("Import execution token is stale")
        if import_job.status != ImportJobStatus.RUNNING:
            raise StaleImportExecutionError("Import job is not running")

    @staticmethod
    def _persist_requests(
        import_job: ImportJob,
        results: Sequence[GroupingResult],
    ) -> tuple[dict[tuple[str, int], PurchaseRequest], int]:
        candidates = [
            result.request_candidate for result in results if result.request_candidate is not None
        ]
        new_requests = [
            PurchaseRequest(
                import_job=import_job,
                source_sheet_name=candidate.source_sheet_name,
                source_start_row_number=candidate.source_start_row_number,
                request_date=candidate.request_date,
                request_date_raw=candidate.request_date_raw,
                customer_name=candidate.customer_name,
                customer_name_raw=candidate.customer_name_raw,
            )
            for candidate in candidates
        ]
        PurchaseRequest.objects.bulk_create(new_requests)

        request_by_origin = {
            (request.source_sheet_name, request.source_start_row_number): request
            for request in new_requests
        }
        required_origins = {
            (result.source_sheet_name, result.request_start_row_number)
            for result in results
            if result.request_start_row_number is not None
        }
        missing_origins = required_origins - request_by_origin.keys()
        if missing_origins:
            start_rows = {row_number for _sheet_name, row_number in missing_origins}
            existing_requests = PurchaseRequest.objects.filter(
                import_job=import_job,
                source_start_row_number__in=start_rows,
            )
            request_by_origin.update(
                {
                    (request.source_sheet_name, request.source_start_row_number): request
                    for request in existing_requests
                    if (request.source_sheet_name, request.source_start_row_number)
                    in missing_origins
                }
            )
        if required_origins - request_by_origin.keys():
            raise StoredCheckpointInvariantError("Batch references a missing purchase request")
        return request_by_origin, len(new_requests)

    @staticmethod
    def _persist_items(
        results: Sequence[GroupingResult],
        request_by_origin: Mapping[tuple[str, int], PurchaseRequest],
    ) -> int:
        items: list[PurchaseRequestItem] = []
        for result in results:
            candidate = result.item_candidate
            if candidate is None or result.request_start_row_number is None:
                continue
            purchase_request = request_by_origin[
                (result.source_sheet_name, result.request_start_row_number)
            ]
            items.append(
                PurchaseRequestItem(
                    purchase_request=purchase_request,
                    source_row_number=candidate.source_row_number,
                    catalog_number=candidate.catalog_number,
                    catalog_number_search_key=candidate.catalog_number_search_key,
                    product_name=candidate.product_name,
                    brand_name=candidate.brand_name,
                    quantity=candidate.quantity,
                    quantity_raw=candidate.quantity_raw,
                    price_amount=candidate.price_amount,
                    price_raw=candidate.price_raw,
                    currency_code=candidate.currency_code,
                    currency_raw=candidate.currency_raw,
                    delivery_time=candidate.delivery_time,
                    delivery_date=candidate.delivery_date,
                    delivery_min_days=candidate.delivery_min_days,
                    delivery_max_days=candidate.delivery_max_days,
                    part_classification=(
                        candidate.part_classification.value
                        if candidate.part_classification is not None
                        else ""
                    ),
                    part_classification_raw=candidate.part_classification_raw,
                )
            )
        PurchaseRequestItem.objects.bulk_create(items)
        return len(items)

    @staticmethod
    def _persist_issues(import_job: ImportJob, results: Sequence[GroupingResult]) -> int:
        issues = [
            ImportRowIssue(
                import_job=import_job,
                source_sheet_name=result.source_sheet_name,
                source_row_number=result.source_row_number,
                severity=ImportIssueSeverity.ERROR,
                issue_codes=[issue.code.value for issue in result.issues],
                messages=[issue.message for issue in result.issues],
                raw_row_snapshot=result.raw_row_snapshot,
            )
            for result in results
            if result.issues
        ]
        ImportRowIssue.objects.bulk_create(issues)
        return len(issues)


def _validate_batch(
    results: Sequence[GroupingResult],
    checkpoint: GrouperCheckpoint,
) -> tuple[int, int]:
    if not results:
        raise ValueError("A persisted batch cannot be empty")
    row_numbers = [result.source_row_number for result in results]
    if any(row_number <= 0 for row_number in row_numbers):
        raise ValueError("Every grouping result must have a positive source row")
    if row_numbers != sorted(row_numbers) or len(row_numbers) != len(set(row_numbers)):
        raise ValueError("Batch source rows must be unique and increasing")
    if any(result.source_sheet_name != checkpoint.sheet_name for result in results):
        raise ValueError("Batch rows and checkpoint must belong to one worksheet")
    return row_numbers[0], row_numbers[-1]


def _batch_fingerprint(
    results: Sequence[GroupingResult],
    checkpoint: GrouperCheckpoint,
) -> str:
    payload = {
        "results": [asdict(result) for result in results],
        "checkpoint": checkpoint.to_dict(),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()
