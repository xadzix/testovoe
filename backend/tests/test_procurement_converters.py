from datetime import date, datetime
from decimal import Decimal

import pytest
from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900, to_excel

from apps.data_imports.contracts import (
    CellPresence,
    ExcelCellValue,
    FormulaResultStatus,
    XlsxSourceCell,
    XlsxSourceRow,
)
from apps.data_imports.header_mapping import CanonicalField, HeaderMapping
from apps.procurement.importing.converters import PurchaseRequestRowConverter
from apps.procurement.importing.schema import IssueCode, PartClassificationValue


def _mapping() -> HeaderMapping:
    return HeaderMapping(
        assignments=tuple((field, column) for column, field in enumerate(CanonicalField, start=1)),
        unrecognized_columns=(),
    )


def _row(
    values: dict[CanonicalField, ExcelCellValue],
    *,
    number_formats: dict[CanonicalField, str] | None = None,
    formula_without_cache: CanonicalField | None = None,
    workbook_epoch: datetime = CALENDAR_WINDOWS_1900,
) -> XlsxSourceRow:
    formats = number_formats or {}
    cells: list[XlsxSourceCell] = []
    for column_number, field in enumerate(CanonicalField, start=1):
        value = values.get(field)
        is_missing_formula = field is formula_without_cache
        if is_missing_formula:
            value = "=A1"
        cells.append(
            XlsxSourceCell(
                sheet_name="Requests",
                coordinate=f"C{column_number}",
                row_number=2,
                column_number=column_number,
                raw_value=value,
                data_type="f" if is_missing_formula else "n",
                number_format=formats.get(field, "General"),
                is_date=isinstance(value, (date, datetime)),
                presence=CellPresence.VALUE if value is not None else CellPresence.MISSING,
                formula_expression="=A1" if is_missing_formula else None,
                formula_cached_value=None,
                formula_result_status=(
                    FormulaResultStatus.MISSING
                    if is_missing_formula
                    else FormulaResultStatus.NOT_FORMULA
                ),
            )
        )
    return XlsxSourceRow("Requests", 2, tuple(cells), workbook_epoch)


def _base_values() -> dict[CanonicalField, ExcelCellValue]:
    return {
        CanonicalField.REQUEST_DATE: date(2026, 10, 21),
        CanonicalField.CUSTOMER_NAME: " ACME  Parts ",
        CanonicalField.CATALOG_NUMBER: "001-AB",
        CanonicalField.PRODUCT_NAME: "Bearing",
    }


@pytest.mark.parametrize("epoch", [CALENDAR_WINDOWS_1900, CALENDAR_MAC_1904])
def test_converts_numeric_excel_date_using_workbook_epoch(epoch: datetime) -> None:
    values = _base_values()
    values[CanonicalField.REQUEST_DATE] = to_excel(date(2025, 5, 17), epoch)

    converted = PurchaseRequestRowConverter(_mapping()).convert(_row(values, workbook_epoch=epoch))

    assert converted.request_fields.request_date == date(2025, 5, 17)
    assert converted.request_fields.issues == ()


def test_uses_native_datetime_without_excel_serial_conversion() -> None:
    values = _base_values()
    values[CanonicalField.REQUEST_DATE] = datetime(2025, 5, 17, 13, 45)

    converted = PurchaseRequestRowConverter(_mapping()).convert(_row(values))

    assert converted.request_fields.request_date == date(2025, 5, 17)


def test_normalizes_customer_and_preserves_display_value() -> None:
    converted = PurchaseRequestRowConverter(_mapping()).convert(_row(_base_values()))

    assert converted.request_fields.customer_name == "ACME Parts"
    assert converted.request_fields.customer_comparison_key == "acme parts"
    assert converted.request_fields.customer_name_raw == " ACME  Parts "


def test_converts_decimal_without_binary_float_expansion() -> None:
    values = _base_values()
    values[CanonicalField.QUANTITY] = 0.1
    values[CanonicalField.PRICE_AMOUNT] = "1 234,5678 RUB"

    converted = PurchaseRequestRowConverter(_mapping()).convert(_row(values))

    assert converted.item_candidate is not None
    assert converted.item_candidate.quantity == Decimal("0.1")
    assert converted.item_candidate.price_amount == Decimal("1234.5678")
    assert converted.item_candidate.currency_code == "RUB"
    assert converted.item_candidate.currency_raw == "RUB"


def test_reconstructs_simple_leading_zero_catalog_format() -> None:
    values = _base_values()
    values[CanonicalField.CATALOG_NUMBER] = 73

    converted = PurchaseRequestRowConverter(_mapping()).convert(
        _row(values, number_formats={CanonicalField.CATALOG_NUMBER: "000000"})
    )

    assert converted.item_candidate is not None
    assert converted.item_candidate.catalog_number == "000073"


def test_converts_delivery_range_and_classification() -> None:
    values = _base_values()
    values[CanonicalField.DELIVERY_TIME] = "5\u201310 дней"
    values[CanonicalField.PART_CLASSIFICATION] = " Аналог "

    converted = PurchaseRequestRowConverter(_mapping()).convert(_row(values))

    assert converted.item_candidate is not None
    assert converted.item_candidate.delivery_min_days == 5
    assert converted.item_candidate.delivery_max_days == 10
    assert converted.item_candidate.part_classification is PartClassificationValue.ANALOGUE


def test_reports_invalid_date_quantity_and_classification() -> None:
    values = _base_values()
    values[CanonicalField.REQUEST_DATE] = "05/06/2025"
    values[CanonicalField.QUANTITY] = "many"
    values[CanonicalField.PART_CLASSIFICATION] = "unknown"

    converted = PurchaseRequestRowConverter(_mapping()).convert(_row(values))

    assert {issue.code for issue in converted.request_fields.issues} == {
        IssueCode.INVALID_REQUEST_DATE
    }
    assert {issue.code for issue in converted.item_conversion_issues} == {
        IssueCode.INVALID_QUANTITY,
        IssueCode.INVALID_PART_CLASSIFICATION,
    }


def test_reports_formula_without_cached_value() -> None:
    converted = PurchaseRequestRowConverter(_mapping()).convert(
        _row(_base_values(), formula_without_cache=CanonicalField.PRICE_AMOUNT)
    )

    assert converted.item_candidate is not None
    assert [issue.code for issue in converted.item_conversion_issues] == [
        IssueCode.FORMULA_CACHED_VALUE_MISSING
    ]
