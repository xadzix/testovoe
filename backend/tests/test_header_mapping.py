import pytest

from apps.data_imports.contracts import (
    CellPresence,
    FormulaResultStatus,
    XlsxSourceCell,
    XlsxSourceRow,
)
from apps.data_imports.header_mapping import (
    CanonicalField,
    DuplicateHeaderError,
    HeaderMapper,
    MissingHeadersError,
)

STANDARD_HEADERS = (
    "date of request",
    "mark",
    "catalog number",
    "product name",
    "brand",
    "quantity",
    "price",
    "delivery time",
    "original / analogue",
)


def _header_row(headers: tuple[str | None, ...]) -> XlsxSourceRow:
    cells = tuple(
        XlsxSourceCell(
            sheet_name="Requests",
            coordinate=f"column-{column_number}",
            row_number=1,
            column_number=column_number,
            raw_value=value,
            data_type="s" if value is not None else "n",
            number_format="General",
            is_date=False,
            presence=CellPresence.VALUE if value is not None else CellPresence.MISSING,
            formula_expression=None,
            formula_cached_value=None,
            formula_result_status=FormulaResultStatus.NOT_FORMULA,
        )
        for column_number, value in enumerate(headers, start=1)
    )
    return XlsxSourceRow(sheet_name="Requests", row_number=1, cells=cells)


def test_maps_standard_headers() -> None:
    mapping = HeaderMapper().map(_header_row(STANDARD_HEADERS))

    assert mapping.column_for(CanonicalField.REQUEST_DATE) == 1
    assert mapping.column_for(CanonicalField.CUSTOMER_NAME) == 2
    assert mapping.column_for(CanonicalField.PART_CLASSIFICATION) == 9
    assert mapping.unrecognized_columns == ()


def test_maps_reordered_aliases_and_reports_unknown_columns() -> None:
    headers = (
        "Срок поставки",
        "Артикул",
        "Количество",
        "Наименование заказчика",
        "Дата заявки",
        "Производитель",
        "Стоимость",
        "Оригинал/Аналог",
        "Наименование товара",
        "Комментарий",
    )

    mapping = HeaderMapper().map(_header_row(headers))

    assert mapping.column_for(CanonicalField.DELIVERY_TIME) == 1
    assert mapping.column_for(CanonicalField.CATALOG_NUMBER) == 2
    assert mapping.column_for(CanonicalField.CUSTOMER_NAME) == 4
    assert mapping.column_for(CanonicalField.PRODUCT_NAME) == 9
    assert mapping.unrecognized_columns == (10,)


def test_normalizes_case_whitespace_and_slash_spacing() -> None:
    headers = (*STANDARD_HEADERS[:-1], "  ORIGINAL   /   ANALOGUE  ")

    mapping = HeaderMapper().map(_header_row(headers))

    assert mapping.column_for(CanonicalField.PART_CLASSIFICATION) == 9


def test_rejects_missing_required_header() -> None:
    headers = STANDARD_HEADERS[:-1]

    with pytest.raises(MissingHeadersError) as error_info:
        HeaderMapper().map(_header_row(headers))

    assert error_info.value.missing_fields == (CanonicalField.PART_CLASSIFICATION,)


def test_rejects_duplicate_canonical_header() -> None:
    headers = (*STANDARD_HEADERS, "customer name")

    with pytest.raises(DuplicateHeaderError) as error_info:
        HeaderMapper().map(_header_row(headers))

    assert error_info.value.field is CanonicalField.CUSTOMER_NAME
    assert error_info.value.first_column == 2
    assert error_info.value.duplicate_column == 10
