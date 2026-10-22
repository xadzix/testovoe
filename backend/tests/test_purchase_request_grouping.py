from datetime import date
from decimal import Decimal

import pytest

from apps.procurement.importing.grouping import (
    CheckpointInvariantError,
    PurchaseRequestRowGrouper,
)
from apps.procurement.importing.schema import (
    ConvertedRequestFields,
    ConvertedRow,
    GrouperCheckpoint,
    GroupingDecision,
    GroupState,
    ImportIssue,
    IssueCode,
    PurchaseRequestItemCandidate,
)

D1 = date(2026, 1, 10)
D2 = date(2026, 1, 11)
C1 = "Customer One"
C2 = "Customer Two"


def _item(row_number: int, *, quantity: Decimal = Decimal("1")) -> PurchaseRequestItemCandidate:
    return PurchaseRequestItemCandidate(
        source_row_number=row_number,
        catalog_number=f"PART-{row_number}",
        catalog_number_search_key=f"part-{row_number}",
        product_name="Part",
        brand_name="Brand",
        quantity=quantity,
        quantity_raw=str(quantity),
        price_amount=None,
        price_raw="",
        currency_code="",
        currency_raw="",
        delivery_time="",
        delivery_date=None,
        delivery_min_days=None,
        delivery_max_days=None,
        part_classification=None,
        part_classification_raw="",
    )


def _row(
    row_number: int,
    *,
    request_date: date | None = None,
    date_provided: bool = False,
    customer_name: str | None = None,
    customer_provided: bool = False,
    request_issues: tuple[ImportIssue, ...] = (),
    item: PurchaseRequestItemCandidate | None = None,
    item_issues: tuple[ImportIssue, ...] = (),
    is_empty: bool = False,
) -> ConvertedRow:
    customer_key = customer_name.casefold() if customer_name is not None else None
    return ConvertedRow(
        sheet_name="Requests",
        row_number=row_number,
        is_empty=is_empty,
        request_fields=ConvertedRequestFields(
            request_date=request_date,
            request_date_raw=request_date.isoformat() if request_date is not None else "",
            request_date_provided=date_provided,
            customer_name=customer_name,
            customer_name_raw=customer_name or "",
            customer_comparison_key=customer_key,
            customer_name_provided=customer_provided,
            issues=request_issues,
        ),
        item_candidate=item,
        item_conversion_issues=item_issues,
    )


def _first_row() -> ConvertedRow:
    return _row(
        2,
        request_date=D1,
        date_provided=True,
        customer_name=C1,
        customer_provided=True,
    )


@pytest.mark.parametrize(
    ("request_date", "date_provided", "customer", "customer_provided", "decision"),
    [
        (None, False, None, False, GroupingDecision.CONTINUE_REQUEST),
        (D1, True, None, False, GroupingDecision.CONTINUE_REQUEST),
        (None, False, C1, True, GroupingDecision.CONTINUE_REQUEST),
        (D1, True, C1, True, GroupingDecision.CONTINUE_REQUEST),
        (D2, True, None, False, GroupingDecision.START_REQUEST),
        (None, False, C2, True, GroupingDecision.START_REQUEST),
        (D2, True, C1, True, GroupingDecision.START_REQUEST),
        (D1, True, C2, True, GroupingDecision.START_REQUEST),
        (D2, True, C2, True, GroupingDecision.START_REQUEST),
    ],
)
def test_valid_context_transition_matrix(
    request_date: date | None,
    date_provided: bool,
    customer: str | None,
    customer_provided: bool,
    decision: GroupingDecision,
) -> None:
    grouper = PurchaseRequestRowGrouper()
    grouper.process(_first_row())

    result = grouper.process(
        _row(
            3,
            request_date=request_date,
            date_provided=date_provided,
            customer_name=customer,
            customer_provided=customer_provided,
        )
    )

    assert result.decision is decision
    expected_start_row = 3 if decision is GroupingDecision.START_REQUEST else 2
    assert result.request_start_row_number == expected_start_row
    if result.request_candidate is not None:
        assert result.request_candidate.request_date == (request_date or D1)
        assert result.request_candidate.customer_name == (customer or C1)


@pytest.mark.parametrize(
    ("row", "expected_code"),
    [
        (
            _row(2, request_date=D1, date_provided=True, item=_item(2)),
            IssueCode.MISSING_CUSTOMER_NAME_WITHOUT_CONTEXT,
        ),
        (
            _row(2, customer_name=C1, customer_provided=True, item=_item(2)),
            IssueCode.MISSING_REQUEST_DATE_WITHOUT_CONTEXT,
        ),
        (
            _row(2, item=_item(2)),
            IssueCode.MISSING_REQUEST_DATE_WITHOUT_CONTEXT,
        ),
    ],
)
def test_first_partial_or_orphan_row_enters_invalid_context(
    row: ConvertedRow,
    expected_code: IssueCode,
) -> None:
    result = PurchaseRequestRowGrouper().process(row)

    assert result.decision is GroupingDecision.INVALID_CONTEXT
    assert result.request_start_row_number is None
    assert result.item_candidate is None
    assert expected_code in {issue.code for issue in result.issues}
    assert IssueCode.ORPHAN_ITEM in {issue.code for issue in result.issues}


def test_completely_empty_row_does_not_clear_valid_context() -> None:
    grouper = PurchaseRequestRowGrouper()
    grouper.process(_first_row())

    skipped = grouper.process(_row(3, is_empty=True))
    continued = grouper.process(_row(4, item=_item(4)))

    assert skipped.decision is GroupingDecision.SKIP
    assert continued.decision is GroupingDecision.CONTINUE_REQUEST
    assert continued.request_start_row_number == 2


@pytest.mark.parametrize(
    ("field_name", "code"),
    [
        ("request_date", IssueCode.INVALID_REQUEST_DATE),
        ("customer_name", IssueCode.INVALID_CUSTOMER_NAME),
    ],
)
def test_invalid_common_value_quarantines_item_from_previous_request(
    field_name: str,
    code: IssueCode,
) -> None:
    grouper = PurchaseRequestRowGrouper()
    grouper.process(_first_row())
    invalid_issue = ImportIssue(code, field_name, "invalid")

    result = grouper.process(_row(3, request_issues=(invalid_issue,), item=_item(3)))

    assert grouper.state is GroupState.ACTIVE_INVALID_REQUEST
    assert result.request_start_row_number is None
    assert result.item_candidate is None
    assert IssueCode.ORPHAN_ITEM in {issue.code for issue in result.issues}


def test_invalid_context_does_not_combine_partial_rows_and_recovers_from_full_row() -> None:
    grouper = PurchaseRequestRowGrouper()
    grouper.process(_row(2, request_date=D1, date_provided=True, item=_item(2)))

    still_invalid = grouper.process(
        _row(3, customer_name=C1, customer_provided=True, item=_item(3))
    )
    recovered = grouper.process(
        _row(
            4,
            request_date=D2,
            date_provided=True,
            customer_name=C2,
            customer_provided=True,
            item=_item(4),
        )
    )

    assert still_invalid.decision is GroupingDecision.INVALID_CONTEXT
    assert still_invalid.request_start_row_number is None
    assert recovered.decision is GroupingDecision.START_REQUEST
    assert recovered.request_start_row_number == 4
    assert recovered.item_candidate is not None


def test_invalid_item_does_not_destroy_valid_request_context() -> None:
    grouper = PurchaseRequestRowGrouper()
    grouper.process(_first_row())

    invalid_item = grouper.process(_row(3, item=_item(3, quantity=Decimal("-1"))))
    next_item = grouper.process(_row(4, item=_item(4)))

    assert invalid_item.decision is GroupingDecision.CONTINUE_REQUEST
    assert invalid_item.item_candidate is None
    assert IssueCode.QUANTITY_MUST_BE_POSITIVE in {issue.code for issue in invalid_item.issues}
    assert next_item.item_candidate is not None
    assert next_item.request_start_row_number == 2
    assert grouper.state is GroupState.ACTIVE_VALID_REQUEST


def test_restores_valid_checkpoint_and_continues_current_request() -> None:
    grouper = PurchaseRequestRowGrouper()
    first_result = grouper.process(_first_row())
    assert first_result.request_candidate is not None
    serialized = grouper.checkpoint().to_dict()

    checkpoint = GrouperCheckpoint.from_dict(serialized)
    restored = PurchaseRequestRowGrouper.from_checkpoint(
        checkpoint,
        first_result.request_candidate,
    )
    result = restored.process(_row(3, item=_item(3)))

    assert result.decision is GroupingDecision.CONTINUE_REQUEST
    assert result.request_start_row_number == 2
    assert result.item_candidate is not None


def test_rejects_valid_checkpoint_without_matching_persisted_request() -> None:
    grouper = PurchaseRequestRowGrouper()
    grouper.process(_first_row())

    with pytest.raises(CheckpointInvariantError):
        PurchaseRequestRowGrouper.from_checkpoint(grouper.checkpoint())


def test_restores_invalid_checkpoint_without_previous_valid_request() -> None:
    grouper = PurchaseRequestRowGrouper()
    grouper.process(_row(2, item=_item(2)))
    serialized = grouper.checkpoint().to_dict()

    restored = PurchaseRequestRowGrouper.from_checkpoint(GrouperCheckpoint.from_dict(serialized))
    invalid_result = restored.process(_row(3, item=_item(3)))
    recovered_result = restored.process(
        _row(
            4,
            request_date=D1,
            date_provided=True,
            customer_name=C1,
            customer_provided=True,
        )
    )

    assert invalid_result.request_start_row_number is None
    assert recovered_result.decision is GroupingDecision.START_REQUEST
    assert recovered_result.request_start_row_number == 4
