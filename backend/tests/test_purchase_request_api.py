from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.data_imports.models import ImportJob, ImportJobStatus
from apps.procurement.models import PurchaseRequest, PurchaseRequestItem

pytestmark = pytest.mark.django_db


def _import_job(status: str = ImportJobStatus.COMPLETED) -> ImportJob:
    return ImportJob.objects.create(
        status=status,
        original_filename="requests.xlsx",
        storage_key="imports/requests.xlsx",
        source_byte_size=1024,
        source_sha256="a" * 64,
        security_scope="local",
        configuration_snapshot={},
        configuration_hash="b" * 64,
        importer_version="1",
    )


def _purchase_request(
    import_job: ImportJob,
    row_number: int,
    *,
    customer_name: str = "Acme",
    request_date: date = date(2026, 10, 21),
) -> PurchaseRequest:
    return PurchaseRequest.objects.create(
        import_job=import_job,
        source_sheet_name="Requests",
        source_start_row_number=row_number,
        request_date=request_date,
        request_date_raw=request_date.isoformat(),
        customer_name=customer_name,
        customer_name_raw=customer_name,
    )


def _item(
    purchase_request: PurchaseRequest,
    row_number: int,
    *,
    catalog_number: str,
    quantity: Decimal | None = Decimal("1"),
    price_amount: Decimal | None = Decimal("1"),
    currency_code: str = "RUB",
) -> PurchaseRequestItem:
    return PurchaseRequestItem.objects.create(
        purchase_request=purchase_request,
        source_row_number=row_number,
        catalog_number=catalog_number,
        product_name=f"Part {catalog_number}",
        quantity=quantity,
        price_amount=price_amount,
        currency_code=currency_code,
    )


def test_list_is_paginated_filtered_and_hides_unpublished_imports(client: Client) -> None:
    first_job = _import_job()
    second_job = _import_job(ImportJobStatus.COMPLETED_WITH_ISSUES)
    failed_job = _import_job(ImportJobStatus.FAILED)
    first = _purchase_request(first_job, 2, customer_name="Acme North")
    second = _purchase_request(first_job, 3, customer_name="Acme South")
    _purchase_request(second_job, 2, customer_name="Other")
    _purchase_request(failed_job, 2, customer_name="Acme hidden")
    _item(first, 2, catalog_number="A")
    _item(first, 3, catalog_number="B")
    _item(second, 4, catalog_number="C")

    response = client.get(
        "/api/v1/purchase-requests/",
        {
            "import_job_id": str(first_job.id),
            "request_date": "2026-10-21",
            "customer_name": "acme",
            "page": 2,
            "page_size": 1,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert payload["next"] is None
    assert payload["previous"] is not None
    assert payload["results"] == [
        {
            "id": second.id,
            "import_job_id": str(first_job.id),
            "source_sheet_name": "Requests",
            "source_start_row_number": 3,
            "request_date": "2026-10-21",
            "customer_name": "Acme South",
            "item_count": 1,
        }
    ]


def test_list_uses_constant_query_count(client: Client) -> None:
    import_job = _import_job()
    for request_number in range(5):
        purchase_request = _purchase_request(import_job, request_number + 2)
        for item_number in range(3):
            _item(
                purchase_request,
                request_number * 10 + item_number + 2,
                catalog_number=f"P-{request_number}-{item_number}",
            )

    with CaptureQueriesContext(connection) as queries:
        response = client.get("/api/v1/purchase-requests/?page_size=10")

    assert response.status_code == 200
    assert len(queries) == 2
    assert [result["item_count"] for result in response.json()["results"]] == [3] * 5


def test_detail_links_to_paginated_items_and_preserves_decimals(client: Client) -> None:
    purchase_request = _purchase_request(_import_job(), 2)
    _item(
        purchase_request,
        2,
        catalog_number="0001-A",
        quantity=Decimal("12345678901234.123456"),
        price_amount=Decimal("1234567890123456.1234"),
    )
    _item(purchase_request, 3, catalog_number="0002-B", quantity=None, price_amount=None)

    detail_response = client.get(f"/api/v1/purchase-requests/{purchase_request.id}/")
    items_response = client.get(
        f"/api/v1/purchase-requests/{purchase_request.id}/items/?page=1&page_size=1"
    )

    assert detail_response.status_code == 200
    assert detail_response.json()["item_count"] == 2
    assert detail_response.json()["items_url"].endswith(f"/{purchase_request.id}/items/")
    assert items_response.status_code == 200
    payload = items_response.json()
    assert payload["count"] == 2
    assert payload["next"] is not None
    assert payload["results"][0]["catalog_number"] == "0001-A"
    assert payload["results"][0]["quantity"] == "12345678901234.123456"
    assert payload["results"][0]["price_amount"] == "1234567890123456.1234"


def test_completed_with_issues_request_is_published(client: Client) -> None:
    purchase_request = _purchase_request(_import_job(ImportJobStatus.COMPLETED_WITH_ISSUES), 2)

    response = client.get(f"/api/v1/purchase-requests/{purchase_request.id}/")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "status",
    [ImportJobStatus.QUEUED, ImportJobStatus.RUNNING, ImportJobStatus.FAILED],
)
def test_detail_hides_requests_from_unpublished_imports(client: Client, status: str) -> None:
    purchase_request = _purchase_request(_import_job(status), 2)

    response = client.get(f"/api/v1/purchase-requests/{purchase_request.id}/")

    assert response.status_code == 404


def test_summary_aggregates_valid_values_without_mixing_currencies(client: Client) -> None:
    import_job = _import_job()
    purchase_request = _purchase_request(import_job, 2)
    _purchase_request(import_job, 20, customer_name="No valid items")
    _item(
        purchase_request,
        2,
        catalog_number="A",
        quantity=Decimal("2"),
        price_amount=Decimal("3"),
    )
    _item(
        purchase_request,
        3,
        catalog_number="A",
        quantity=Decimal("1.5"),
        price_amount=Decimal("4"),
    )
    _item(
        purchase_request,
        4,
        catalog_number="B",
        quantity=Decimal("5"),
        price_amount=Decimal("2"),
        currency_code="USD",
    )
    _item(purchase_request, 5, catalog_number="C", quantity=None, price_amount=Decimal("3"))
    _item(purchase_request, 6, catalog_number="D", quantity=Decimal("4"), price_amount=None)
    _item(
        purchase_request,
        7,
        catalog_number="E",
        quantity=Decimal("2"),
        price_amount=Decimal("7"),
        currency_code="",
    )
    hidden_request = _purchase_request(_import_job(ImportJobStatus.FAILED), 2)
    _item(hidden_request, 2, catalog_number="HIDDEN", quantity=Decimal("100"))
    other_request = _purchase_request(_import_job(), 2)
    _item(other_request, 2, catalog_number="OTHER", quantity=Decimal("100"))

    response = client.get(
        "/api/v1/purchase-requests/summary/",
        {"import_job_id": str(import_job.id)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["request_count"] == 1
    assert payload["item_count"] == 6
    assert payload["total_quantity"] == "14.500000"
    assert payload["missing_quantity_item_count"] == 1
    assert payload["excluded_from_cost_item_count"] == 3
    assert payload["costs_by_currency"] == [
        {"currency_code": "RUB", "amount": "12.0000000000"},
        {"currency_code": "USD", "amount": "10.0000000000"},
    ]
    assert payload["top_catalog_numbers"] == [
        {"catalog_number": "B", "total_quantity": "5.000000"},
        {"catalog_number": "D", "total_quantity": "4.000000"},
        {"catalog_number": "A", "total_quantity": "3.500000"},
        {"catalog_number": "E", "total_quantity": "2.000000"},
    ]


@pytest.mark.parametrize(
    ("path", "query", "error"),
    [
        ("/api/v1/purchase-requests/", {"request_date": "21.10.2026"}, "invalid_request_date"),
        ("/api/v1/purchase-requests/", {"import_job_id": "bad"}, "invalid_import_job_id"),
        ("/api/v1/purchase-requests/summary/", {"import_job_id": "bad"}, "invalid_import_job_id"),
        ("/api/v1/purchase-requests/", {"page_size": "101"}, "invalid_pagination"),
    ],
)
def test_invalid_query_parameters_return_400(
    client: Client,
    path: str,
    query: dict[str, str],
    error: str,
) -> None:
    response = client.get(path, query)

    assert response.status_code == 400
    assert response.json() == {"error": error}
