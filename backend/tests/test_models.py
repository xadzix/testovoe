from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

from apps.data_imports.models import (
    ImportIssueSeverity,
    ImportJob,
    ImportJobStatus,
    ImportRowIssue,
)
from apps.procurement.models import PartClassification, PurchaseRequest, PurchaseRequestItem

pytestmark = pytest.mark.django_db


def create_import_job(
    *,
    security_scope: str = "tenant-a",
    idempotency_key: str = "",
    source_sha256: str = "a" * 64,
    configuration_hash: str = "b" * 64,
) -> ImportJob:
    return ImportJob.objects.create(
        original_filename="requests.xlsx",
        storage_key="imports/requests.xlsx",
        source_byte_size=1024,
        source_sha256=source_sha256,
        security_scope=security_scope,
        idempotency_key=idempotency_key,
        configuration_snapshot={"sheet": "Requests"},
        configuration_hash=configuration_hash,
        importer_version="1",
        selected_sheet_name="Requests",
    )


def create_purchase_request(
    import_job: ImportJob,
    *,
    source_start_row_number: int = 2,
    customer_name: str = "Acme",
) -> PurchaseRequest:
    return PurchaseRequest.objects.create(
        import_job=import_job,
        source_sheet_name="Requests",
        source_start_row_number=source_start_row_number,
        request_date=date(2026, 10, 21),
        request_date_raw="21.10.2026",
        customer_name=customer_name,
        customer_name_raw=customer_name,
    )


def test_identical_files_can_create_distinct_jobs_without_idempotency_key() -> None:
    first_job = create_import_job()
    second_job = create_import_job()

    assert first_job.pk != second_job.pk


def test_idempotency_key_is_unique_only_inside_security_scope() -> None:
    create_import_job(idempotency_key="request-42")

    with pytest.raises(IntegrityError), transaction.atomic():
        create_import_job(
            idempotency_key="request-42",
            source_sha256="c" * 64,
            configuration_hash="d" * 64,
        )

    other_scope_job = create_import_job(
        security_scope="tenant-b",
        idempotency_key="request-42",
    )
    assert other_scope_job.pk is not None


def test_import_job_status_is_checked_by_postgresql() -> None:
    import_job = create_import_job()

    with pytest.raises(IntegrityError), transaction.atomic():
        ImportJob.objects.filter(pk=import_job.pk).update(status="unknown")

    import_job.refresh_from_db()
    assert import_job.status == ImportJobStatus.QUEUED


def test_same_customer_and_date_can_have_multiple_requests() -> None:
    import_job = create_import_job()

    first_request = create_purchase_request(import_job, source_start_row_number=2)
    second_request = create_purchase_request(import_job, source_start_row_number=8)

    assert first_request.request_date == second_request.request_date
    assert first_request.customer_name == second_request.customer_name


def test_purchase_request_source_origin_is_unique() -> None:
    import_job = create_import_job()
    create_purchase_request(import_job, source_start_row_number=2)

    with pytest.raises(IntegrityError), transaction.atomic():
        create_purchase_request(import_job, source_start_row_number=2)


def test_request_protects_job_and_request_deletion_cascades_to_items_and_issues() -> None:
    import_job = create_import_job()
    purchase_request = create_purchase_request(import_job)
    item = PurchaseRequestItem.objects.create(
        purchase_request=purchase_request,
        source_row_number=2,
        product_name="Bearing",
    )
    issue = ImportRowIssue.objects.create(
        import_job=import_job,
        source_sheet_name="Requests",
        source_row_number=3,
        severity=ImportIssueSeverity.WARNING,
        issue_codes=["missing_brand"],
        messages=["Brand is missing"],
    )

    with pytest.raises(ProtectedError):
        import_job.delete()

    purchase_request.delete()
    assert not PurchaseRequestItem.objects.filter(pk=item.pk).exists()

    import_job.delete()
    assert not ImportRowIssue.objects.filter(pk=issue.pk).exists()


def test_item_source_origin_is_unique_within_request() -> None:
    purchase_request = create_purchase_request(create_import_job())
    PurchaseRequestItem.objects.create(
        purchase_request=purchase_request,
        source_row_number=2,
        catalog_number="0001-A",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        PurchaseRequestItem.objects.create(
            purchase_request=purchase_request,
            source_row_number=2,
            product_name="Duplicate row",
        )


def test_decimal_values_round_trip_without_precision_loss() -> None:
    purchase_request = create_purchase_request(create_import_job())
    quantity = Decimal("12345678901234.123456")
    price_amount = Decimal("1234567890123456.1234")

    item = PurchaseRequestItem.objects.create(
        purchase_request=purchase_request,
        source_row_number=2,
        product_name="Precision test",
        quantity=quantity,
        price_amount=price_amount,
    )
    item.refresh_from_db()

    assert isinstance(item.quantity, Decimal)
    assert isinstance(item.price_amount, Decimal)
    assert item.quantity == quantity
    assert item.price_amount == price_amount


def test_decimal_field_validation_rejects_excess_precision() -> None:
    item = PurchaseRequestItem(
        purchase_request=create_purchase_request(create_import_job()),
        source_row_number=2,
        product_name="Precision test",
        quantity=Decimal("1.0000001"),
        price_amount=Decimal("1.00001"),
    )

    with pytest.raises(ValidationError) as error:
        item.full_clean()

    assert {"quantity", "price_amount"} <= error.value.message_dict.keys()


def test_quantity_and_price_ranges_are_checked_by_postgresql() -> None:
    purchase_request = create_purchase_request(create_import_job())

    with pytest.raises(IntegrityError), transaction.atomic():
        PurchaseRequestItem.objects.create(
            purchase_request=purchase_request,
            source_row_number=2,
            product_name="Invalid quantity",
            quantity=Decimal("0"),
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        PurchaseRequestItem.objects.create(
            purchase_request=purchase_request,
            source_row_number=3,
            product_name="Invalid price",
            price_amount=Decimal("-0.0001"),
        )


def test_item_requires_catalog_number_or_product_name() -> None:
    purchase_request = create_purchase_request(create_import_job())

    with pytest.raises(IntegrityError), transaction.atomic():
        PurchaseRequestItem.objects.create(
            purchase_request=purchase_request,
            source_row_number=2,
        )


@pytest.mark.parametrize(
    ("delivery_date", "delivery_min_days", "delivery_max_days"),
    [
        (date(2026, 11, 1), 1, 2),
        (None, 1, None),
        (None, None, 2),
        (None, 3, 2),
    ],
)
def test_inconsistent_delivery_normalization_is_rejected(
    delivery_date: date | None,
    delivery_min_days: int | None,
    delivery_max_days: int | None,
) -> None:
    purchase_request = create_purchase_request(create_import_job())

    with pytest.raises(IntegrityError), transaction.atomic():
        PurchaseRequestItem.objects.create(
            purchase_request=purchase_request,
            source_row_number=2,
            product_name="Delivery test",
            delivery_date=delivery_date,
            delivery_min_days=delivery_min_days,
            delivery_max_days=delivery_max_days,
        )


def test_classification_and_currency_are_checked_by_postgresql() -> None:
    purchase_request = create_purchase_request(create_import_job())

    with pytest.raises(IntegrityError), transaction.atomic():
        PurchaseRequestItem.objects.create(
            purchase_request=purchase_request,
            source_row_number=2,
            product_name="Invalid classification",
            part_classification="replacement",
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        PurchaseRequestItem.objects.create(
            purchase_request=purchase_request,
            source_row_number=3,
            product_name="Invalid currency",
            part_classification=PartClassification.ORIGINAL,
            currency_code="usd",
        )


def test_row_issue_source_origin_and_severity_are_checked_by_postgresql() -> None:
    import_job = create_import_job()
    ImportRowIssue.objects.create(
        import_job=import_job,
        source_sheet_name="Requests",
        source_row_number=2,
        severity=ImportIssueSeverity.ERROR,
        issue_codes=["invalid_quantity"],
        messages=["Quantity is invalid"],
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        ImportRowIssue.objects.create(
            import_job=import_job,
            source_sheet_name="Requests",
            source_row_number=2,
            severity=ImportIssueSeverity.WARNING,
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        ImportRowIssue.objects.create(
            import_job=import_job,
            source_sheet_name="Requests",
            source_row_number=3,
            severity="info",
        )
