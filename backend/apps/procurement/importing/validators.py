from decimal import Decimal

from apps.procurement.importing.schema import (
    ImportIssue,
    IssueCode,
    PurchaseRequestCandidate,
    PurchaseRequestItemCandidate,
)


def validate_request_candidate(candidate: PurchaseRequestCandidate) -> tuple[ImportIssue, ...]:
    if len(candidate.customer_name) > 500:
        return (_issue(IssueCode.CUSTOMER_NAME_TOO_LONG, "customer_name"),)
    return ()


def validate_item_candidate(candidate: PurchaseRequestItemCandidate) -> tuple[ImportIssue, ...]:
    issues: list[ImportIssue] = []
    if not candidate.catalog_number and not candidate.product_name:
        issues.append(_issue(IssueCode.ITEM_IDENTITY_REQUIRED, "catalog_number"))
    if len(candidate.catalog_number) > 255:
        issues.append(_issue(IssueCode.CATALOG_NUMBER_TOO_LONG, "catalog_number"))
    if len(candidate.brand_name) > 255:
        issues.append(_issue(IssueCode.BRAND_NAME_TOO_LONG, "brand_name"))

    if candidate.quantity is not None:
        if candidate.quantity <= 0:
            issues.append(_issue(IssueCode.QUANTITY_MUST_BE_POSITIVE, "quantity"))
        elif not decimal_fits(candidate.quantity, max_digits=20, decimal_places=6):
            issues.append(_issue(IssueCode.QUANTITY_PRECISION_EXCEEDED, "quantity"))

    if candidate.price_amount is not None:
        if candidate.price_amount < 0:
            issues.append(_issue(IssueCode.PRICE_MUST_BE_NONNEGATIVE, "price_amount"))
        elif not decimal_fits(candidate.price_amount, max_digits=20, decimal_places=4):
            issues.append(_issue(IssueCode.PRICE_PRECISION_EXCEEDED, "price_amount"))

    if (
        candidate.delivery_min_days is not None
        and candidate.delivery_max_days is not None
        and (
            candidate.delivery_min_days < 0
            or candidate.delivery_min_days > candidate.delivery_max_days
        )
    ):
        issues.append(_issue(IssueCode.INVALID_DELIVERY_RANGE, "delivery_time"))
    return tuple(issues)


def decimal_fits(value: Decimal, *, max_digits: int, decimal_places: int) -> bool:
    """Проверить точную представимость значения целевым DecimalField."""
    if not value.is_finite():
        return False
    _, source_digits, source_exponent = value.as_tuple()
    digits = source_digits
    exponent = source_exponent
    while digits and digits[-1] == 0:
        digits = digits[:-1]
        exponent += 1
    if not digits:
        return True
    fractional_digits = max(-exponent, 0)
    integer_digits = max(len(digits) + exponent, 0)
    return fractional_digits <= decimal_places and integer_digits <= max_digits - decimal_places


def _issue(code: IssueCode, field_name: str) -> ImportIssue:
    return ImportIssue(code, field_name, code.value.replace("_", " "))
