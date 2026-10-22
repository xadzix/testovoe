import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from apps.data_imports.contracts import XlsxSourceRow


class CanonicalField(StrEnum):
    REQUEST_DATE = "request_date"
    CUSTOMER_NAME = "customer_name"
    CATALOG_NUMBER = "catalog_number"
    PRODUCT_NAME = "product_name"
    BRAND_NAME = "brand_name"
    QUANTITY = "quantity"
    PRICE_AMOUNT = "price_amount"
    DELIVERY_TIME = "delivery_time"
    PART_CLASSIFICATION = "part_classification"


@dataclass(frozen=True, slots=True)
class HeaderMapping:
    assignments: tuple[tuple[CanonicalField, int], ...]
    unrecognized_columns: tuple[int, ...]

    def column_for(self, field: CanonicalField) -> int:
        for assigned_field, column_number in self.assignments:
            if assigned_field is field:
                return column_number
        raise KeyError(field)


class HeaderMappingError(ValueError):
    """Базовая ошибка некорректной строки заголовков XLSX."""


class MissingHeadersError(HeaderMappingError):
    def __init__(self, missing_fields: tuple[CanonicalField, ...]) -> None:
        self.missing_fields = missing_fields
        field_names = ", ".join(field.value for field in missing_fields)
        super().__init__(f"Missing required headers: {field_names}")


class DuplicateHeaderError(HeaderMappingError):
    def __init__(
        self,
        field: CanonicalField,
        first_column: int,
        duplicate_column: int,
    ) -> None:
        self.field = field
        self.first_column = first_column
        self.duplicate_column = duplicate_column
        super().__init__(
            f"Header {field.value!r} occurs in columns {first_column} and {duplicate_column}"
        )


_HEADER_ALIASES: dict[CanonicalField, frozenset[str]] = {
    CanonicalField.REQUEST_DATE: frozenset({"date of request", "request date", "дата заявки"}),
    CanonicalField.CUSTOMER_NAME: frozenset(
        {"mark", "customer", "customer name", "заказчик", "наименование заказчика"}
    ),
    CanonicalField.CATALOG_NUMBER: frozenset(
        {"catalog number", "catalogue number", "part number", "артикул", "каталожный номер"}
    ),
    CanonicalField.PRODUCT_NAME: frozenset(
        {"product name", "name", "наименование", "наименование товара"}
    ),
    CanonicalField.BRAND_NAME: frozenset({"brand", "manufacturer", "бренд", "производитель"}),
    CanonicalField.QUANTITY: frozenset({"quantity", "qty", "количество", "кол-во"}),
    CanonicalField.PRICE_AMOUNT: frozenset({"price", "amount", "цена", "стоимость"}),
    CanonicalField.DELIVERY_TIME: frozenset({"delivery time", "lead time", "срок поставки"}),
    CanonicalField.PART_CLASSIFICATION: frozenset(
        {
            "original / analogue",
            "original/analogue",
            "original or analogue",
            "оригинал / аналог",
            "оригинал/аналог",
        }
    ),
}


def _normalize_header(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = re.sub(r"\s*/\s*", "/", normalized)
    return " ".join(normalized.split())


_FIELD_BY_NORMALIZED_ALIAS = {
    _normalize_header(alias): field
    for field, aliases in _HEADER_ALIASES.items()
    for alias in aliases
}


class HeaderMapper:
    """Сопоставить строку заголовков XLSX девяти полям импорта."""

    def map(self, header_row: XlsxSourceRow) -> HeaderMapping:
        column_by_field: dict[CanonicalField, int] = {}
        unrecognized_columns: list[int] = []

        for cell in header_row.cells:
            if not isinstance(cell.raw_value, str) or not cell.raw_value.strip():
                if cell.raw_value is not None:
                    unrecognized_columns.append(cell.column_number)
                continue

            field = _FIELD_BY_NORMALIZED_ALIAS.get(_normalize_header(cell.raw_value))
            if field is None:
                unrecognized_columns.append(cell.column_number)
                continue

            previous_column = column_by_field.get(field)
            if previous_column is not None:
                raise DuplicateHeaderError(field, previous_column, cell.column_number)
            column_by_field[field] = cell.column_number

        missing_fields = tuple(field for field in CanonicalField if field not in column_by_field)
        if missing_fields:
            raise MissingHeadersError(missing_fields)

        assignments = tuple((field, column_by_field[field]) for field in CanonicalField)
        return HeaderMapping(assignments, tuple(unrecognized_columns))
