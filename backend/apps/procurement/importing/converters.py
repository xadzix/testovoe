import math
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from openpyxl.utils.datetime import from_excel

from apps.data_imports.contracts import (
    ExcelCellValue,
    FormulaResultStatus,
    XlsxSourceCell,
    XlsxSourceRow,
)
from apps.data_imports.header_mapping import CanonicalField, HeaderMapping
from apps.procurement.importing.schema import (
    ConvertedRequestFields,
    ConvertedRow,
    ImportIssue,
    IssueCode,
    PartClassificationValue,
    PurchaseRequestItemCandidate,
)

_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y")
_DECIMAL_PATTERN = re.compile(r"^[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?$")
_PRICE_PATTERN = re.compile(
    r"^\s*"
    r"(?P<prefix>[A-Za-z]{3}|₽|€|\$)?\s*"
    r"(?P<amount>[+-]?\d[\d\s\u00a0]*(?:[.,]\d+)?(?:[eE][+-]?\d+)?)"
    r"\s*(?P<suffix>[A-Za-z]{3}|₽|€|\$)?"
    r"\s*$"
)
_DAY_COUNT_PATTERN = re.compile(r"^(\d+)\s*(?:d|day|days|д|дн|день|дня|дней)?$", re.IGNORECASE)
_DAY_RANGE_PATTERN = re.compile(
    r"^(\d+)\s*[-\u2013\u2014]\s*(\d+)\s*(?:d|day|days|д|дн|день|дня|дней)?$",
    re.IGNORECASE,
)
_CURRENCY_BY_SYMBOL = {"₽": "RUB", "$": "USD", "€": "EUR"}
_CLASSIFICATION_ALIASES = {
    "original": PartClassificationValue.ORIGINAL,
    "оригинал": PartClassificationValue.ORIGINAL,
    "oem": PartClassificationValue.ORIGINAL,
    "analogue": PartClassificationValue.ANALOGUE,
    "analog": PartClassificationValue.ANALOGUE,
    "аналог": PartClassificationValue.ANALOGUE,
}


class PurchaseRequestRowConverter:
    """Преобразовать сопоставленную строку без применения правил группировки."""

    def __init__(self, header_mapping: HeaderMapping, default_currency_code: str = "") -> None:
        self._header_mapping = header_mapping
        self._default_currency_code = default_currency_code.strip().upper()

    def convert(self, row: XlsxSourceRow) -> ConvertedRow:
        mapped_cells = {field: self._cell(row, field) for field in CanonicalField}
        request_fields = self._convert_request_fields(row, mapped_cells)
        item_candidate, item_issues = self._convert_item(row, mapped_cells)
        return ConvertedRow(
            sheet_name=row.sheet_name,
            row_number=row.row_number,
            is_empty=all(_is_blank_cell(cell) for cell in mapped_cells.values()),
            request_fields=request_fields,
            item_candidate=item_candidate,
            item_conversion_issues=item_issues,
            raw_row_snapshot={field.value: _raw_text(cell) for field, cell in mapped_cells.items()},
        )

    def _cell(self, row: XlsxSourceRow, field: CanonicalField) -> XlsxSourceCell | None:
        column_number = self._header_mapping.column_for(field)
        if column_number > len(row.cells):
            return None
        return row.cells[column_number - 1]

    def _convert_request_fields(
        self,
        row: XlsxSourceRow,
        cells: dict[CanonicalField, XlsxSourceCell | None],
    ) -> ConvertedRequestFields:
        date_cell = cells[CanonicalField.REQUEST_DATE]
        customer_cell = cells[CanonicalField.CUSTOMER_NAME]
        request_date, date_provided, date_issues = _request_date(date_cell, row.workbook_epoch)
        customer_name, customer_key, customer_provided, customer_issues = _customer_name(
            customer_cell
        )
        return ConvertedRequestFields(
            request_date=request_date,
            request_date_raw=_raw_text(date_cell),
            request_date_provided=date_provided,
            customer_name=customer_name,
            customer_name_raw=_raw_text(customer_cell),
            customer_comparison_key=customer_key,
            customer_name_provided=customer_provided,
            issues=(*date_issues, *customer_issues),
        )

    def _convert_item(
        self,
        row: XlsxSourceRow,
        cells: dict[CanonicalField, XlsxSourceCell | None],
    ) -> tuple[PurchaseRequestItemCandidate | None, tuple[ImportIssue, ...]]:
        item_fields = (
            CanonicalField.CATALOG_NUMBER,
            CanonicalField.PRODUCT_NAME,
            CanonicalField.BRAND_NAME,
            CanonicalField.QUANTITY,
            CanonicalField.PRICE_AMOUNT,
            CanonicalField.DELIVERY_TIME,
            CanonicalField.PART_CLASSIFICATION,
        )
        if all(_is_blank_cell(cells[field]) for field in item_fields):
            return None, ()

        catalog_number, catalog_issues = _catalog_number(cells[CanonicalField.CATALOG_NUMBER])
        product_name, product_issues = _text_field(
            cells[CanonicalField.PRODUCT_NAME],
            CanonicalField.PRODUCT_NAME,
        )
        brand_name, brand_issues = _text_field(
            cells[CanonicalField.BRAND_NAME],
            CanonicalField.BRAND_NAME,
        )
        quantity, quantity_issues = _decimal_field(
            cells[CanonicalField.QUANTITY],
            CanonicalField.QUANTITY,
            IssueCode.INVALID_QUANTITY,
        )
        price_amount, currency_code, currency_raw, price_issues = self._price(
            cells[CanonicalField.PRICE_AMOUNT]
        )
        delivery_time, delivery_date, delivery_min_days, delivery_max_days, delivery_issues = (
            _delivery_time(cells[CanonicalField.DELIVERY_TIME])
        )
        classification, classification_issues = _part_classification(
            cells[CanonicalField.PART_CLASSIFICATION]
        )
        issues = (
            *catalog_issues,
            *product_issues,
            *brand_issues,
            *quantity_issues,
            *price_issues,
            *delivery_issues,
            *classification_issues,
        )
        return (
            PurchaseRequestItemCandidate(
                source_row_number=row.row_number,
                catalog_number=catalog_number,
                catalog_number_search_key=_comparison_text(catalog_number),
                product_name=product_name,
                brand_name=brand_name,
                quantity=quantity,
                quantity_raw=_raw_text(cells[CanonicalField.QUANTITY]),
                price_amount=price_amount,
                price_raw=_raw_text(cells[CanonicalField.PRICE_AMOUNT]),
                currency_code=currency_code,
                currency_raw=currency_raw,
                delivery_time=delivery_time,
                delivery_date=delivery_date,
                delivery_min_days=delivery_min_days,
                delivery_max_days=delivery_max_days,
                part_classification=classification,
                part_classification_raw=_raw_text(cells[CanonicalField.PART_CLASSIFICATION]),
            ),
            issues,
        )

    def _price(
        self,
        cell: XlsxSourceCell | None,
    ) -> tuple[Decimal | None, str, str, tuple[ImportIssue, ...]]:
        value, formula_issue = _usable_value(cell, CanonicalField.PRICE_AMOUNT)
        if formula_issue is not None:
            return None, "", "", (formula_issue,)
        if value is None or (isinstance(value, str) and not value.strip()):
            return None, "", "", ()

        currency_raw = ""
        decimal_source: ExcelCellValue = value
        if isinstance(value, str):
            match = _PRICE_PATTERN.fullmatch(value)
            if match is None:
                return None, "", "", (_issue(IssueCode.INVALID_PRICE, "price_amount"),)

            prefix = match.group("prefix")
            suffix = match.group("suffix")

            if prefix and suffix:
                return None, "", "", (_issue(IssueCode.INVALID_PRICE, "price_amount"),)

            decimal_source = match.group("amount")
            currency_raw = prefix or suffix or ""

        price_amount = _to_decimal(decimal_source)
        if price_amount is None:
            return None, "", currency_raw, (_issue(IssueCode.INVALID_PRICE, "price_amount"),)

        currency_code = _CURRENCY_BY_SYMBOL.get(currency_raw, currency_raw.upper())
        if not currency_code:
            currency_code = self._default_currency_code
        if currency_code and not re.fullmatch(r"[A-Z]{3}", currency_code):
            return (
                price_amount,
                "",
                currency_raw,
                (_issue(IssueCode.INVALID_CURRENCY, "currency_code"),),
            )
        return price_amount, currency_code, currency_raw, ()


def _request_date(
    cell: XlsxSourceCell | None,
    workbook_epoch: datetime | None,
) -> tuple[date | None, bool, tuple[ImportIssue, ...]]:
    value, formula_issue = _usable_value(cell, CanonicalField.REQUEST_DATE)
    if formula_issue is not None:
        return None, True, (formula_issue,)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, False, ()
    if isinstance(value, datetime):
        return value.date(), True, ()
    if isinstance(value, date):
        return value, True, ()
    if isinstance(value, bool):
        return None, True, (_issue(IssueCode.INVALID_REQUEST_DATE, "request_date"),)
    if isinstance(value, (int, float)):
        if workbook_epoch is None or (isinstance(value, float) and not math.isfinite(value)):
            return None, True, (_issue(IssueCode.INVALID_REQUEST_DATE, "request_date"),)
        try:
            converted = from_excel(value, epoch=workbook_epoch)
        except (OverflowError, ValueError):
            return None, True, (_issue(IssueCode.INVALID_REQUEST_DATE, "request_date"),)
        if isinstance(converted, datetime):
            return converted.date(), True, ()
        if isinstance(converted, date):
            return converted, True, ()
        return None, True, (_issue(IssueCode.INVALID_REQUEST_DATE, "request_date"),)
    if isinstance(value, str):
        for date_format in _DATE_FORMATS:
            try:
                return datetime.strptime(value.strip(), date_format).date(), True, ()
            except ValueError:
                continue
    return None, True, (_issue(IssueCode.INVALID_REQUEST_DATE, "request_date"),)


def _customer_name(
    cell: XlsxSourceCell | None,
) -> tuple[str | None, str | None, bool, tuple[ImportIssue, ...]]:
    value, formula_issue = _usable_value(cell, CanonicalField.CUSTOMER_NAME)
    if formula_issue is not None:
        return None, None, True, (formula_issue,)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None, False, ()
    if isinstance(value, (date, datetime, bool)):
        return None, None, True, (_issue(IssueCode.INVALID_CUSTOMER_NAME, "customer_name"),)
    if isinstance(value, float) and not math.isfinite(value):
        return None, None, True, (_issue(IssueCode.INVALID_CUSTOMER_NAME, "customer_name"),)

    customer_name = _display_text(str(value))
    if not customer_name:
        return None, None, False, ()
    return customer_name, _comparison_text(customer_name), True, ()


def _catalog_number(
    cell: XlsxSourceCell | None,
) -> tuple[str, tuple[ImportIssue, ...]]:
    value, formula_issue = _usable_value(cell, CanonicalField.CATALOG_NUMBER)
    if formula_issue is not None:
        return "", (formula_issue,)
    if value is None or (isinstance(value, str) and not value.strip()):
        return "", ()
    if isinstance(value, (bool, date, datetime)):
        return "", (_issue(IssueCode.ITEM_IDENTITY_REQUIRED, "catalog_number"),)
    if isinstance(value, int):
        number_format = cell.number_format if cell is not None else ""
        if value >= 0 and re.fullmatch(r"0+", number_format):
            return str(value).zfill(len(number_format)), ()
        return str(value), ()
    if isinstance(value, float):
        if not math.isfinite(value):
            return "", (_issue(IssueCode.ITEM_IDENTITY_REQUIRED, "catalog_number"),)
        if value.is_integer():
            integer_value = int(value)
            number_format = cell.number_format if cell is not None else ""
            if integer_value >= 0 and re.fullmatch(r"0+", number_format):
                return str(integer_value).zfill(len(number_format)), ()
            return str(integer_value), ()
        return str(Decimal(str(value))), ()
    return _display_text(_raw_scalar_text(value)), ()


def _text_field(
    cell: XlsxSourceCell | None,
    field: CanonicalField,
) -> tuple[str, tuple[ImportIssue, ...]]:
    value, formula_issue = _usable_value(cell, field)
    if formula_issue is not None:
        return "", (formula_issue,)
    if value is None:
        return "", ()
    return _display_text(_raw_scalar_text(value)), ()


def _decimal_field(
    cell: XlsxSourceCell | None,
    field: CanonicalField,
    invalid_code: IssueCode,
) -> tuple[Decimal | None, tuple[ImportIssue, ...]]:
    value, formula_issue = _usable_value(cell, field)
    if formula_issue is not None:
        return None, (formula_issue,)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, ()
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return None, (_issue(invalid_code, field.value),)
    return decimal_value, ()


def _to_decimal(value: ExcelCellValue) -> Decimal | None:
    if isinstance(value, (bool, date, datetime)) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Decimal(str(value))
    if not isinstance(value, str):
        return None
    normalized = value.replace("\u00a0", "").replace(" ", "")
    if not _DECIMAL_PATTERN.fullmatch(normalized):
        return None
    normalized = normalized.replace(",", ".")
    try:
        decimal_value = Decimal(normalized)
    except InvalidOperation:
        return None
    return decimal_value if decimal_value.is_finite() else None


def _delivery_time(
    cell: XlsxSourceCell | None,
) -> tuple[str, date | None, int | None, int | None, tuple[ImportIssue, ...]]:
    value, formula_issue = _usable_value(cell, CanonicalField.DELIVERY_TIME)
    if formula_issue is not None:
        return "", None, None, None, (formula_issue,)
    if value is None or (isinstance(value, str) and not value.strip()):
        return "", None, None, None, ()
    if isinstance(value, datetime):
        return value.isoformat(), value.date(), None, None, ()
    if isinstance(value, date):
        return value.isoformat(), value, None, None, ()
    if isinstance(value, bool):
        return str(value), None, None, None, ()
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        days = int(value)
        return str(value), None, days, days, ()

    delivery_time = _display_text(str(value))
    for date_format in _DATE_FORMATS:
        try:
            delivery_date = datetime.strptime(delivery_time, date_format).date()
            return delivery_time, delivery_date, None, None, ()
        except ValueError:
            continue
    range_match = _DAY_RANGE_PATTERN.fullmatch(delivery_time)
    if range_match is not None:
        return delivery_time, None, int(range_match.group(1)), int(range_match.group(2)), ()
    count_match = _DAY_COUNT_PATTERN.fullmatch(delivery_time)
    if count_match is not None:
        days = int(count_match.group(1))
        return delivery_time, None, days, days, ()
    return delivery_time, None, None, None, ()


def _part_classification(
    cell: XlsxSourceCell | None,
) -> tuple[PartClassificationValue | None, tuple[ImportIssue, ...]]:
    value, formula_issue = _usable_value(cell, CanonicalField.PART_CLASSIFICATION)
    if formula_issue is not None:
        return None, (formula_issue,)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, ()
    if not isinstance(value, str):
        return None, (_issue(IssueCode.INVALID_PART_CLASSIFICATION, "part_classification"),)
    classification = _CLASSIFICATION_ALIASES.get(_comparison_text(value))
    if classification is None:
        return None, (_issue(IssueCode.INVALID_PART_CLASSIFICATION, "part_classification"),)
    return classification, ()


def _usable_value(
    cell: XlsxSourceCell | None,
    field: CanonicalField,
) -> tuple[ExcelCellValue, ImportIssue | None]:
    if cell is None:
        return None, None
    if cell.formula_result_status is FormulaResultStatus.MISSING:
        return None, _issue(IssueCode.FORMULA_CACHED_VALUE_MISSING, field.value)
    return cell.value_for_conversion, None


def _is_blank_cell(cell: XlsxSourceCell | None) -> bool:
    if cell is None:
        return True
    if cell.formula_result_status is FormulaResultStatus.MISSING:
        return False
    value = cell.value_for_conversion
    return value is None or (isinstance(value, str) and not value.strip())


def _raw_text(cell: XlsxSourceCell | None) -> str:
    if cell is None or cell.raw_value is None:
        return ""
    return _raw_scalar_text(cell.raw_value)


def _raw_scalar_text(value: ExcelCellValue) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _display_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split())


def normalize_customer_comparison_key(value: str) -> str:
    return _display_text(value).casefold()


def _comparison_text(value: str) -> str:
    return normalize_customer_comparison_key(value)


def _issue(code: IssueCode, field_name: str) -> ImportIssue:
    return ImportIssue(code, field_name, code.value.replace("_", " "))
