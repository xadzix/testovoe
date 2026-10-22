from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum


class IssueCode(StrEnum):
    FORMULA_CACHED_VALUE_MISSING = "formula_cached_value_missing"
    INVALID_REQUEST_DATE = "invalid_request_date"
    MISSING_REQUEST_DATE_WITHOUT_CONTEXT = "missing_request_date_without_context"
    INVALID_CUSTOMER_NAME = "invalid_customer_name"
    CUSTOMER_NAME_TOO_LONG = "customer_name_too_long"
    MISSING_CUSTOMER_NAME_WITHOUT_CONTEXT = "missing_customer_name_without_context"
    ORPHAN_ITEM = "orphan_item"
    CATALOG_NUMBER_TOO_LONG = "catalog_number_too_long"
    ITEM_IDENTITY_REQUIRED = "item_identity_required"
    BRAND_NAME_TOO_LONG = "brand_name_too_long"
    INVALID_QUANTITY = "invalid_quantity"
    QUANTITY_MUST_BE_POSITIVE = "quantity_must_be_positive"
    QUANTITY_PRECISION_EXCEEDED = "quantity_precision_exceeded"
    INVALID_PRICE = "invalid_price"
    PRICE_MUST_BE_NONNEGATIVE = "price_must_be_nonnegative"
    PRICE_PRECISION_EXCEEDED = "price_precision_exceeded"
    INVALID_CURRENCY = "invalid_currency"
    INVALID_DELIVERY_RANGE = "invalid_delivery_range"
    INVALID_PART_CLASSIFICATION = "invalid_part_classification"


class PartClassificationValue(StrEnum):
    ORIGINAL = "original"
    ANALOGUE = "analogue"


class GroupState(StrEnum):
    NO_ACTIVE_REQUEST = "no_active_request"
    ACTIVE_VALID_REQUEST = "active_valid_request"
    ACTIVE_INVALID_REQUEST = "active_invalid_request"


class GroupingDecision(StrEnum):
    SKIP = "skip"
    START_REQUEST = "start_request"
    CONTINUE_REQUEST = "continue_request"
    INVALID_CONTEXT = "invalid_context"


@dataclass(frozen=True, slots=True)
class ImportIssue:
    code: IssueCode
    field_name: str
    message: str


@dataclass(frozen=True, slots=True)
class ConvertedRequestFields:
    request_date: date | None
    request_date_raw: str
    request_date_provided: bool
    customer_name: str | None
    customer_name_raw: str
    customer_comparison_key: str | None
    customer_name_provided: bool
    issues: tuple[ImportIssue, ...]


@dataclass(frozen=True, slots=True)
class PurchaseRequestCandidate:
    source_sheet_name: str
    source_start_row_number: int
    request_date: date
    request_date_raw: str
    customer_name: str
    customer_name_raw: str
    customer_comparison_key: str


@dataclass(frozen=True, slots=True)
class PurchaseRequestItemCandidate:
    source_row_number: int
    catalog_number: str
    catalog_number_search_key: str
    product_name: str
    brand_name: str
    quantity: Decimal | None
    quantity_raw: str
    price_amount: Decimal | None
    price_raw: str
    currency_code: str
    currency_raw: str
    delivery_time: str
    delivery_date: date | None
    delivery_min_days: int | None
    delivery_max_days: int | None
    part_classification: PartClassificationValue | None
    part_classification_raw: str


@dataclass(frozen=True, slots=True)
class ConvertedRow:
    sheet_name: str
    row_number: int
    is_empty: bool
    request_fields: ConvertedRequestFields
    item_candidate: PurchaseRequestItemCandidate | None
    item_conversion_issues: tuple[ImportIssue, ...]
    raw_row_snapshot: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GroupingResult:
    decision: GroupingDecision
    request_candidate: PurchaseRequestCandidate | None
    request_start_row_number: int | None
    item_candidate: PurchaseRequestItemCandidate | None
    issues: tuple[ImportIssue, ...]
    source_sheet_name: str = ""
    source_row_number: int = 0
    raw_row_snapshot: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GrouperCheckpoint:
    version: int
    sheet_name: str
    group_state: GroupState
    source_start_row_number: int | None = None
    error_codes: tuple[IssueCode, ...] = ()

    def to_dict(self) -> dict[str, object]:
        checkpoint: dict[str, object] = {
            "version": self.version,
            "sheet_name": self.sheet_name,
            "group_state": self.group_state.value,
        }
        if self.group_state is GroupState.ACTIVE_VALID_REQUEST:
            checkpoint["current_request"] = {
                "source_start_row_number": self.source_start_row_number,
            }
        elif self.group_state is GroupState.ACTIVE_INVALID_REQUEST:
            checkpoint["invalid_group"] = {
                "source_start_row_number": self.source_start_row_number,
                "error_codes": [code.value for code in self.error_codes],
            }
        return checkpoint

    @classmethod
    def from_dict(cls, checkpoint: Mapping[str, object]) -> "GrouperCheckpoint":
        version = checkpoint.get("version")
        sheet_name = checkpoint.get("sheet_name")
        raw_group_state = checkpoint.get("group_state")
        if version != 1 or not isinstance(sheet_name, str) or not isinstance(raw_group_state, str):
            raise ValueError("Invalid grouper checkpoint header")

        group_state = GroupState(raw_group_state)
        source_start_row_number: int | None = None
        error_codes: tuple[IssueCode, ...] = ()

        if group_state is GroupState.ACTIVE_VALID_REQUEST:
            current_request = checkpoint.get("current_request")
            if not isinstance(current_request, Mapping):
                raise ValueError("Valid checkpoint is missing current_request")
            source_start_row_number = _positive_row_number(current_request)
        elif group_state is GroupState.ACTIVE_INVALID_REQUEST:
            invalid_group = checkpoint.get("invalid_group")
            if not isinstance(invalid_group, Mapping):
                raise ValueError("Invalid checkpoint is missing invalid_group")
            source_start_row_number = _positive_row_number(invalid_group)
            raw_error_codes = invalid_group.get("error_codes")
            if not isinstance(raw_error_codes, list) or not all(
                isinstance(code, str) for code in raw_error_codes
            ):
                raise ValueError("Invalid checkpoint error codes")
            error_codes = tuple(IssueCode(code) for code in raw_error_codes)

        return cls(version, sheet_name, group_state, source_start_row_number, error_codes)


def _positive_row_number(checkpoint_section: Mapping[str, object]) -> int:
    row_number = checkpoint_section.get("source_start_row_number")
    if not isinstance(row_number, int) or isinstance(row_number, bool) or row_number <= 0:
        raise ValueError("Checkpoint source row number must be positive")
    return row_number
