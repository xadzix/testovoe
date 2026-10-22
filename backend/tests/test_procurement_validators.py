from dataclasses import replace
from decimal import Decimal

from apps.procurement.importing.schema import PurchaseRequestItemCandidate
from apps.procurement.importing.validators import decimal_fits, validate_item_candidate


def _item() -> PurchaseRequestItemCandidate:
    return PurchaseRequestItemCandidate(
        source_row_number=2,
        catalog_number="001-AB",
        catalog_number_search_key="001-ab",
        product_name="Bearing",
        brand_name="SKF",
        quantity=Decimal("1"),
        quantity_raw="1",
        price_amount=Decimal("10.25"),
        price_raw="10.25",
        currency_code="RUB",
        currency_raw="RUB",
        delivery_time="5 days",
        delivery_date=None,
        delivery_min_days=5,
        delivery_max_days=5,
        part_classification=None,
        part_classification_raw="",
    )


def test_decimal_precision_boundaries_match_models() -> None:
    assert decimal_fits(Decimal("99999999999999.999999"), max_digits=20, decimal_places=6)
    assert not decimal_fits(Decimal("100000000000000.000000"), max_digits=20, decimal_places=6)
    assert not decimal_fits(Decimal("1.0000001"), max_digits=20, decimal_places=6)
    assert decimal_fits(Decimal("9999999999999999.9999"), max_digits=20, decimal_places=4)
    assert not decimal_fits(Decimal("0.00001"), max_digits=20, decimal_places=4)


def test_decimal_precision_allows_exact_trailing_zero_rescale() -> None:
    assert decimal_fits(Decimal("1.2300000"), max_digits=20, decimal_places=6)
    assert not decimal_fits(Decimal("1" + "0" * 100), max_digits=20, decimal_places=6)


def test_validates_empty_and_too_long_catalog_numbers() -> None:
    missing_identity = replace(_item(), catalog_number="", product_name="")
    too_long = replace(_item(), catalog_number="A" * 256)

    missing_codes = {issue.code.value for issue in validate_item_candidate(missing_identity)}
    long_codes = {issue.code.value for issue in validate_item_candidate(too_long)}

    assert missing_codes == {"item_identity_required"}
    assert long_codes == {"catalog_number_too_long"}


def test_validates_quantity_price_and_delivery_range() -> None:
    candidate = replace(
        _item(),
        quantity=Decimal("-1"),
        price_amount=Decimal("-0.01"),
        delivery_min_days=10,
        delivery_max_days=5,
    )

    codes = {issue.code.value for issue in validate_item_candidate(candidate)}

    assert codes == {
        "quantity_must_be_positive",
        "price_must_be_nonnegative",
        "invalid_delivery_range",
    }
