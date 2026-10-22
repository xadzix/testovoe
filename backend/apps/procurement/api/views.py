from datetime import date
from decimal import Decimal
from uuid import UUID

from django.db.models import (
    Count,
    DecimalField,
    ExpressionWrapper,
    F,
    Q,
    QuerySet,
    Sum,
)
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET

from apps.data_imports.models import ImportJobStatus
from apps.procurement.models import PurchaseRequest, PurchaseRequestItem

_PUBLISHED_IMPORT_STATUSES = (
    ImportJobStatus.COMPLETED,
    ImportJobStatus.COMPLETED_WITH_ISSUES,
)
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 100


@require_GET
def list_purchase_requests(request: HttpRequest) -> JsonResponse:
    pagination = _parse_pagination(request)
    if isinstance(pagination, JsonResponse):
        return pagination
    page, page_size = pagination

    purchase_requests = _published_purchase_requests().annotate(item_count=Count("items"))
    filters = _parse_filters(request)
    if isinstance(filters, JsonResponse):
        return filters
    import_job_id, request_date, customer_name = filters
    if import_job_id is not None:
        purchase_requests = purchase_requests.filter(import_job_id=import_job_id)
    if request_date is not None:
        purchase_requests = purchase_requests.filter(request_date=request_date)
    if customer_name:
        purchase_requests = purchase_requests.filter(customer_name__icontains=customer_name)

    purchase_requests = purchase_requests.order_by("id")
    total = purchase_requests.count()
    offset = (page - 1) * page_size
    results = [
        _request_payload(purchase_request, item_count=purchase_request.item_count)
        for purchase_request in purchase_requests[offset : offset + page_size]
    ]
    return JsonResponse(_page_payload(request, results, total, page, page_size))


@require_GET
def get_purchase_request(_request: HttpRequest, purchase_request_id: int) -> JsonResponse:
    purchase_request = get_object_or_404(
        _published_purchase_requests().annotate(item_count=Count("items")),
        pk=purchase_request_id,
    )
    payload = _request_payload(purchase_request, item_count=purchase_request.item_count)
    payload["items_url"] = f"/api/v1/purchase-requests/{purchase_request.id}/items/"
    return JsonResponse(payload)


@require_GET
def list_purchase_request_items(
    request: HttpRequest,
    purchase_request_id: int,
) -> JsonResponse:
    pagination = _parse_pagination(request)
    if isinstance(pagination, JsonResponse):
        return pagination
    page, page_size = pagination
    purchase_request = get_object_or_404(
        _published_purchase_requests(),
        pk=purchase_request_id,
    )
    items = purchase_request.items.order_by("source_row_number", "id")
    total = items.count()
    offset = (page - 1) * page_size
    results = [_item_payload(item) for item in items[offset : offset + page_size]]
    return JsonResponse(_page_payload(request, results, total, page, page_size))


@require_GET
def get_purchase_request_summary(request: HttpRequest) -> JsonResponse:
    """Агрегировать опубликованные данные, не смешивая суммы разных валют."""

    raw_import_job_id = request.GET.get("import_job_id", "").strip()
    import_job_id = _parse_uuid(raw_import_job_id)
    if raw_import_job_id and import_job_id is None:
        return JsonResponse({"error": "invalid_import_job_id"}, status=400)

    purchase_requests = _published_purchase_requests()
    if import_job_id is not None:
        purchase_requests = purchase_requests.filter(import_job_id=import_job_id)
    items = PurchaseRequestItem.objects.filter(purchase_request__in=purchase_requests)

    request_count = purchase_requests.filter(items__isnull=False).distinct().count()
    item_totals = items.aggregate(
        item_count=Count("id"),
        total_quantity=Sum("quantity"),
        missing_quantity_item_count=Count("id", filter=Q(quantity__isnull=True)),
        excluded_from_cost_item_count=Count(
            "id",
            filter=(Q(quantity__isnull=True) | Q(price_amount__isnull=True) | Q(currency_code="")),
        ),
    )

    line_cost = ExpressionWrapper(
        F("quantity") * F("price_amount"),
        output_field=DecimalField(max_digits=40, decimal_places=10),
    )
    costs_by_currency = [
        {
            "currency_code": row["currency_code"],
            "amount": _decimal_text(row["amount"]),
        }
        for row in items.filter(
            quantity__isnull=False,
            price_amount__isnull=False,
        )
        .exclude(currency_code="")
        .values("currency_code")
        .annotate(amount=Sum(line_cost))
        .order_by("currency_code")
    ]
    top_catalog_numbers = [
        {
            "catalog_number": row["catalog_number"],
            "total_quantity": _decimal_text(row["total_quantity"]),
        }
        for row in items.filter(quantity__isnull=False)
        .exclude(catalog_number="")
        .values("catalog_number")
        .annotate(total_quantity=Sum("quantity"))
        .order_by("-total_quantity", "catalog_number")[:5]
    ]
    return JsonResponse(
        {
            "request_count": request_count,
            "item_count": item_totals["item_count"],
            "total_quantity": _decimal_text(item_totals["total_quantity"] or Decimal(0)),
            "missing_quantity_item_count": item_totals["missing_quantity_item_count"],
            "excluded_from_cost_item_count": item_totals["excluded_from_cost_item_count"],
            "costs_by_currency": costs_by_currency,
            "top_catalog_numbers": top_catalog_numbers,
        }
    )


def _published_purchase_requests() -> QuerySet[PurchaseRequest]:
    return PurchaseRequest.objects.filter(import_job__status__in=_PUBLISHED_IMPORT_STATUSES)


def _parse_filters(
    request: HttpRequest,
) -> tuple[UUID | None, date | None, str] | JsonResponse:
    raw_import_job_id = request.GET.get("import_job_id", "").strip()
    import_job_id = _parse_uuid(raw_import_job_id)
    if raw_import_job_id and import_job_id is None:
        return JsonResponse({"error": "invalid_import_job_id"}, status=400)

    raw_request_date = request.GET.get("request_date", "").strip()
    request_date = parse_date(raw_request_date) if raw_request_date else None
    if raw_request_date and request_date is None:
        return JsonResponse({"error": "invalid_request_date"}, status=400)
    return import_job_id, request_date, request.GET.get("customer_name", "").strip()


def _parse_uuid(value: str) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _parse_pagination(request: HttpRequest) -> tuple[int, int] | JsonResponse:
    try:
        page = int(request.GET.get("page", "1"))
        page_size = int(request.GET.get("page_size", str(_DEFAULT_PAGE_SIZE)))
    except ValueError:
        return JsonResponse({"error": "invalid_pagination"}, status=400)
    if page < 1 or page_size < 1 or page_size > _MAX_PAGE_SIZE:
        return JsonResponse({"error": "invalid_pagination"}, status=400)
    return page, page_size


def _page_payload(
    request: HttpRequest,
    results: list[dict[str, object]],
    total: int,
    page: int,
    page_size: int,
) -> dict[str, object]:
    return {
        "count": total,
        "page": page,
        "page_size": page_size,
        "next": _page_url(request, page + 1, page_size) if page * page_size < total else None,
        "previous": _page_url(request, page - 1, page_size) if page > 1 else None,
        "results": results,
    }


def _page_url(request: HttpRequest, page: int, page_size: int) -> str:
    query = request.GET.copy()
    query["page"] = str(page)
    query["page_size"] = str(page_size)
    return f"{request.path}?{query.urlencode()}"


def _request_payload(
    purchase_request: PurchaseRequest,
    *,
    item_count: int,
) -> dict[str, object]:
    return {
        "id": purchase_request.id,
        "import_job_id": str(purchase_request.import_job_id),
        "source_sheet_name": purchase_request.source_sheet_name,
        "source_start_row_number": purchase_request.source_start_row_number,
        "request_date": purchase_request.request_date.isoformat(),
        "customer_name": purchase_request.customer_name,
        "item_count": item_count,
    }


def _item_payload(item: PurchaseRequestItem) -> dict[str, object]:
    return {
        "id": item.id,
        "source_row_number": item.source_row_number,
        "catalog_number": item.catalog_number,
        "product_name": item.product_name,
        "brand_name": item.brand_name,
        "quantity": _decimal_text(item.quantity),
        "price_amount": _decimal_text(item.price_amount),
        "currency_code": item.currency_code,
        "delivery_time": item.delivery_time,
        "delivery_date": item.delivery_date.isoformat() if item.delivery_date else None,
        "delivery_min_days": item.delivery_min_days,
        "delivery_max_days": item.delivery_max_days,
        "part_classification": item.part_classification,
    }


def _decimal_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None
