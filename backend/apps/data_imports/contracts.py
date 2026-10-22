from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum

type ExcelCellValue = str | int | float | bool | date | datetime | time | timedelta | None


class CellPresence(StrEnum):
    """Способ представления ячейки в XML рабочего листа."""

    MISSING = "missing"
    EXPLICIT_EMPTY = "explicit_empty"
    VALUE = "value"


class FormulaResultStatus(StrEnum):
    """Доступность сохранённого приложением результата формулы."""

    NOT_FORMULA = "not_formula"
    AVAILABLE = "available"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class XlsxSourceCell:
    sheet_name: str
    coordinate: str
    row_number: int
    column_number: int
    raw_value: ExcelCellValue
    data_type: str
    number_format: str
    is_date: bool
    presence: CellPresence
    formula_expression: str | None
    formula_cached_value: ExcelCellValue
    formula_result_status: FormulaResultStatus

    @property
    def value_for_conversion(self) -> ExcelCellValue:
        """Вернуть значение для конвертации без вычисления формулы."""
        if self.formula_expression is not None:
            return self.formula_cached_value
        return self.raw_value


@dataclass(frozen=True, slots=True)
class XlsxSourceRow:
    sheet_name: str
    row_number: int
    cells: tuple[XlsxSourceCell, ...]
    workbook_epoch: datetime | None = None

    @property
    def is_empty(self) -> bool:
        return all(cell.raw_value is None for cell in self.cells)
