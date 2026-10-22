from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from django.db import IntegrityError, close_old_connections, connections

from apps.data_imports.models import ImportJob, ImportJobStatus, ImportRowIssue
from apps.procurement.importing.schema import (
    GrouperCheckpoint,
    GroupingDecision,
    GroupingResult,
    GroupState,
    ImportIssue,
    IssueCode,
    PurchaseRequestCandidate,
    PurchaseRequestItemCandidate,
)
from apps.procurement.importing.writer import (
    BatchCheckpointConflict,
    PurchaseRequestBatchWriter,
    StaleImportExecutionError,
)
from apps.procurement.models import PurchaseRequest, PurchaseRequestItem

pytestmark = pytest.mark.django_db(transaction=True)


def _job(token: UUID | None = None) -> ImportJob:
    return ImportJob.objects.create(
        status=ImportJobStatus.RUNNING,
        original_filename="requests.xlsx",
        storage_key="imports/requests.xlsx",
        source_byte_size=100,
        source_sha256="a" * 64,
        security_scope="tenant-a",
        configuration_snapshot={},
        configuration_hash="b" * 64,
        importer_version="1",
        selected_sheet_name="Requests",
        execution_token=token or uuid4(),
    )


def _request(row_number: int, customer: str = "ACME") -> PurchaseRequestCandidate:
    return PurchaseRequestCandidate(
        source_sheet_name="Requests",
        source_start_row_number=row_number,
        request_date=date(2026, 10, 21),
        request_date_raw="21.10.2026",
        customer_name=customer,
        customer_name_raw=customer,
        customer_comparison_key=customer.casefold(),
    )


def _item(row_number: int, quantity: Decimal = Decimal("1")) -> PurchaseRequestItemCandidate:
    return PurchaseRequestItemCandidate(
        source_row_number=row_number,
        catalog_number=f"PART-{row_number}",
        catalog_number_search_key=f"part-{row_number}",
        product_name="Part",
        brand_name="Brand",
        quantity=quantity,
        quantity_raw=str(quantity),
        price_amount=Decimal("10.25"),
        price_raw="10.25",
        currency_code="RUB",
        currency_raw="RUB",
        delivery_time="",
        delivery_date=None,
        delivery_min_days=None,
        delivery_max_days=None,
        part_classification=None,
        part_classification_raw="",
    )


def _result(
    row_number: int,
    request_start: int | None,
    *,
    request: PurchaseRequestCandidate | None = None,
    item: PurchaseRequestItemCandidate | None = None,
    issues: tuple[ImportIssue, ...] = (),
) -> GroupingResult:
    return GroupingResult(
        decision=(
            GroupingDecision.START_REQUEST
            if request is not None
            else GroupingDecision.CONTINUE_REQUEST
        ),
        request_candidate=request,
        request_start_row_number=request_start,
        item_candidate=item,
        issues=issues,
        source_sheet_name="Requests",
        source_row_number=row_number,
        raw_row_snapshot={"catalog_number": f"PART-{row_number}"},
    )


def _checkpoint(request_start: int) -> GrouperCheckpoint:
    return GrouperCheckpoint(
        version=1,
        sheet_name="Requests",
        group_state=GroupState.ACTIVE_VALID_REQUEST,
        source_start_row_number=request_start,
    )


def test_request_can_continue_across_batches_and_retry_is_noop() -> None:
    job = _job()
    writer = PurchaseRequestBatchWriter()
    first = [_result(2, 2, request=_request(2), item=_item(2))]

    assert writer.write_batch(
        import_job_id=job.id,
        execution_token=job.execution_token,
        expected_checkpoint_row_number=0,
        results=first,
        checkpoint=_checkpoint(2),
    )
    second = [_result(3, 2, item=_item(3)), _result(4, 2, item=_item(4))]
    assert writer.write_batch(
        import_job_id=job.id,
        execution_token=job.execution_token,
        expected_checkpoint_row_number=2,
        results=second,
        checkpoint=_checkpoint(2),
    )
    assert not writer.write_batch(
        import_job_id=job.id,
        execution_token=job.execution_token,
        expected_checkpoint_row_number=2,
        results=second,
        checkpoint=_checkpoint(2),
    )

    job.refresh_from_db()
    request = PurchaseRequest.objects.get(import_job=job)
    assert request.items.count() == 3
    assert job.processed_row_count == 3
    assert job.imported_request_count == 1
    assert job.imported_item_count == 3
    assert job.checkpoint_row_number == 4


def test_batch_can_contain_multiple_requests_and_an_issue() -> None:
    job = _job()
    issue = ImportIssue(IssueCode.INVALID_QUANTITY, "quantity", "invalid quantity")
    results = [
        _result(2, 2, request=_request(2, "A"), item=_item(2)),
        _result(3, 3, request=_request(3, "B"), item=_item(3)),
        _result(4, 3, issues=(issue,)),
    ]

    PurchaseRequestBatchWriter().write_batch(
        import_job_id=job.id,
        execution_token=job.execution_token,
        expected_checkpoint_row_number=0,
        results=results,
        checkpoint=_checkpoint(3),
    )

    job.refresh_from_db()
    stored_issue = ImportRowIssue.objects.get(import_job=job)
    assert PurchaseRequest.objects.filter(import_job=job).count() == 2
    assert PurchaseRequestItem.objects.filter(purchase_request__import_job=job).count() == 2
    assert stored_issue.source_row_number == 4
    assert stored_issue.raw_row_snapshot == {"catalog_number": "PART-4"}
    assert job.invalid_row_count == 1


def test_changed_retry_identity_is_rejected() -> None:
    job = _job()
    writer = PurchaseRequestBatchWriter()
    original = [_result(2, 2, request=_request(2), item=_item(2))]
    writer.write_batch(
        import_job_id=job.id,
        execution_token=job.execution_token,
        expected_checkpoint_row_number=0,
        results=original,
        checkpoint=_checkpoint(2),
    )

    changed = [_result(2, 2, request=_request(2), item=_item(2, Decimal("2")))]
    with pytest.raises(BatchCheckpointConflict):
        writer.write_batch(
            import_job_id=job.id,
            execution_token=job.execution_token,
            expected_checkpoint_row_number=0,
            results=changed,
            checkpoint=_checkpoint(2),
        )


def test_database_error_rolls_back_requests_items_and_checkpoint() -> None:
    job = _job()
    invalid_item = _item(2, Decimal("0"))

    with pytest.raises(IntegrityError):
        PurchaseRequestBatchWriter().write_batch(
            import_job_id=job.id,
            execution_token=job.execution_token,
            expected_checkpoint_row_number=0,
            results=[_result(2, 2, request=_request(2), item=invalid_item)],
            checkpoint=_checkpoint(2),
        )

    job.refresh_from_db()
    assert not PurchaseRequest.objects.filter(import_job=job).exists()
    assert job.checkpoint_row_number == 0
    assert job.processed_row_count == 0


def test_stale_execution_token_cannot_write() -> None:
    job = _job()

    with pytest.raises(StaleImportExecutionError):
        PurchaseRequestBatchWriter().write_batch(
            import_job_id=job.id,
            execution_token=uuid4(),
            expected_checkpoint_row_number=0,
            results=[_result(2, 2, request=_request(2), item=_item(2))],
            checkpoint=_checkpoint(2),
        )

    assert not PurchaseRequest.objects.filter(import_job=job).exists()


def test_checkpoint_restores_current_request() -> None:
    job = _job()
    writer = PurchaseRequestBatchWriter()
    writer.write_batch(
        import_job_id=job.id,
        execution_token=job.execution_token,
        expected_checkpoint_row_number=0,
        results=[_result(2, 2, request=_request(2), item=None)],
        checkpoint=_checkpoint(2),
    )

    row_number, checkpoint, candidate = writer.load_checkpoint_context(
        import_job_id=job.id,
        execution_token=job.execution_token,
    )

    assert row_number == 2
    assert checkpoint.source_start_row_number == 2
    assert candidate is not None
    assert candidate.customer_comparison_key == "acme"
    assert PurchaseRequestItem.objects.count() == 0


def test_concurrent_batches_for_one_job_cannot_both_commit() -> None:
    job = _job()

    def write(row_number: int) -> str:
        close_old_connections()
        try:
            PurchaseRequestBatchWriter().write_batch(
                import_job_id=job.id,
                execution_token=job.execution_token,
                expected_checkpoint_row_number=0,
                results=[
                    _result(
                        row_number,
                        row_number,
                        request=_request(row_number),
                        item=_item(row_number),
                    )
                ],
                checkpoint=_checkpoint(row_number),
            )
            return "committed"
        except BatchCheckpointConflict:
            return "conflict"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, (2, 3)))

    assert sorted(outcomes) == ["committed", "conflict"]
    assert PurchaseRequest.objects.filter(import_job=job).count() == 1
