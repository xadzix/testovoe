from apps.procurement.importing.schema import (
    ConvertedRow,
    GrouperCheckpoint,
    GroupingDecision,
    GroupingResult,
    GroupState,
    ImportIssue,
    IssueCode,
    PurchaseRequestCandidate,
)
from apps.procurement.importing.validators import (
    validate_item_candidate,
    validate_request_candidate,
)


class CheckpointInvariantError(ValueError):
    """Checkpoint не соответствует сохранённому контексту заявки."""


class PurchaseRequestRowGrouper:
    """Группировать поток строк, сохраняя контекст только текущей заявки."""

    def __init__(self) -> None:
        self._state = GroupState.NO_ACTIVE_REQUEST
        self._sheet_name = ""
        self._current_request: PurchaseRequestCandidate | None = None
        self._invalid_start_row_number: int | None = None
        self._invalid_error_codes: tuple[IssueCode, ...] = ()

    @property
    def state(self) -> GroupState:
        return self._state

    def process(self, row: ConvertedRow) -> GroupingResult:
        self._ensure_same_sheet(row.sheet_name)
        self._sheet_name = row.sheet_name
        if row.is_empty:
            return self._result(row, GroupingDecision.SKIP)

        if row.request_fields.issues:
            return self._enter_invalid_context(row, row.request_fields.issues)

        if self._state is GroupState.ACTIVE_VALID_REQUEST:
            return self._process_with_valid_context(row)
        return self._process_without_valid_context(row)

    def checkpoint(self) -> GrouperCheckpoint:
        if self._state is GroupState.ACTIVE_VALID_REQUEST:
            if self._current_request is None:
                raise CheckpointInvariantError("Valid state has no current request")
            return GrouperCheckpoint(
                version=1,
                sheet_name=self._sheet_name,
                group_state=self._state,
                source_start_row_number=self._current_request.source_start_row_number,
            )
        if self._state is GroupState.ACTIVE_INVALID_REQUEST:
            return GrouperCheckpoint(
                version=1,
                sheet_name=self._sheet_name,
                group_state=self._state,
                source_start_row_number=self._invalid_start_row_number,
                error_codes=self._invalid_error_codes,
            )
        return GrouperCheckpoint(1, self._sheet_name, self._state)

    @classmethod
    def from_checkpoint(
        cls,
        checkpoint: GrouperCheckpoint,
        restored_request: PurchaseRequestCandidate | None = None,
    ) -> "PurchaseRequestRowGrouper":
        grouper = cls()
        grouper._sheet_name = checkpoint.sheet_name
        grouper._state = checkpoint.group_state

        if checkpoint.group_state is GroupState.ACTIVE_VALID_REQUEST:
            if (
                restored_request is None
                or restored_request.source_sheet_name != checkpoint.sheet_name
                or restored_request.source_start_row_number != checkpoint.source_start_row_number
            ):
                raise CheckpointInvariantError(
                    "Restored request does not match checkpoint provenance"
                )
            grouper._current_request = restored_request
        elif checkpoint.group_state is GroupState.ACTIVE_INVALID_REQUEST:
            if checkpoint.source_start_row_number is None:
                raise CheckpointInvariantError("Invalid state has no source start row")
            grouper._invalid_start_row_number = checkpoint.source_start_row_number
            grouper._invalid_error_codes = checkpoint.error_codes
        elif restored_request is not None:
            raise CheckpointInvariantError("Inactive checkpoint cannot restore a request")
        return grouper

    def _process_with_valid_context(self, row: ConvertedRow) -> GroupingResult:
        current_request = self._current_request
        if current_request is None:
            raise CheckpointInvariantError("Valid state has no current request")

        fields = row.request_fields
        request_date = (
            fields.request_date if fields.request_date_provided else current_request.request_date
        )
        customer_name = (
            fields.customer_name if fields.customer_name_provided else current_request.customer_name
        )
        customer_key = (
            fields.customer_comparison_key
            if fields.customer_name_provided
            else current_request.customer_comparison_key
        )
        if request_date is None or customer_name is None or customer_key is None:
            raise CheckpointInvariantError("Converted request fields are inconsistent")

        starts_request = (
            request_date != current_request.request_date
            or customer_key != current_request.customer_comparison_key
        )
        if starts_request:
            candidate = PurchaseRequestCandidate(
                source_sheet_name=row.sheet_name,
                source_start_row_number=row.row_number,
                request_date=request_date,
                request_date_raw=(
                    fields.request_date_raw
                    if fields.request_date_provided
                    else current_request.request_date_raw
                ),
                customer_name=customer_name,
                customer_name_raw=(
                    fields.customer_name_raw
                    if fields.customer_name_provided
                    else current_request.customer_name_raw
                ),
                customer_comparison_key=customer_key,
            )
            request_issues = validate_request_candidate(candidate)
            if request_issues:
                return self._enter_invalid_context(row, request_issues)
            self._activate(candidate)
            return self._valid_result(row, GroupingDecision.START_REQUEST, candidate)

        return self._valid_result(row, GroupingDecision.CONTINUE_REQUEST, None)

    def _process_without_valid_context(self, row: ConvertedRow) -> GroupingResult:
        fields = row.request_fields
        issues: list[ImportIssue] = []
        if not fields.request_date_provided:
            issues.append(_issue(IssueCode.MISSING_REQUEST_DATE_WITHOUT_CONTEXT, "request_date"))
        if not fields.customer_name_provided:
            issues.append(_issue(IssueCode.MISSING_CUSTOMER_NAME_WITHOUT_CONTEXT, "customer_name"))

        if issues or fields.request_date is None or fields.customer_name is None:
            return self._enter_invalid_context(row, tuple(issues))
        if fields.customer_comparison_key is None:
            raise CheckpointInvariantError("Valid customer has no comparison key")

        candidate = PurchaseRequestCandidate(
            source_sheet_name=row.sheet_name,
            source_start_row_number=row.row_number,
            request_date=fields.request_date,
            request_date_raw=fields.request_date_raw,
            customer_name=fields.customer_name,
            customer_name_raw=fields.customer_name_raw,
            customer_comparison_key=fields.customer_comparison_key,
        )
        request_issues = validate_request_candidate(candidate)
        if request_issues:
            return self._enter_invalid_context(row, request_issues)
        self._activate(candidate)
        return self._valid_result(row, GroupingDecision.START_REQUEST, candidate)

    def _valid_result(
        self,
        row: ConvertedRow,
        decision: GroupingDecision,
        request_candidate: PurchaseRequestCandidate | None,
    ) -> GroupingResult:
        current_request = self._current_request
        if current_request is None:
            raise CheckpointInvariantError("Cannot attach an item without a valid request")

        item_issues = row.item_conversion_issues
        if row.item_candidate is not None:
            item_issues = (*item_issues, *validate_item_candidate(row.item_candidate))
        valid_item = row.item_candidate if not item_issues else None
        return GroupingResult(
            decision=decision,
            request_candidate=request_candidate,
            request_start_row_number=current_request.source_start_row_number,
            item_candidate=valid_item,
            issues=item_issues,
            source_sheet_name=row.sheet_name,
            source_row_number=row.row_number,
            raw_row_snapshot=row.raw_row_snapshot,
        )

    def _enter_invalid_context(
        self,
        row: ConvertedRow,
        request_issues: tuple[ImportIssue, ...],
    ) -> GroupingResult:
        if self._state is not GroupState.ACTIVE_INVALID_REQUEST:
            self._invalid_start_row_number = row.row_number
            self._invalid_error_codes = tuple(issue.code for issue in request_issues)
        self._state = GroupState.ACTIVE_INVALID_REQUEST
        self._current_request = None

        issues = [*request_issues, *row.item_conversion_issues]
        if row.item_candidate is not None:
            issues.extend(validate_item_candidate(row.item_candidate))
            issues.append(_issue(IssueCode.ORPHAN_ITEM, "purchase_request"))
        return GroupingResult(
            decision=GroupingDecision.INVALID_CONTEXT,
            request_candidate=None,
            request_start_row_number=None,
            item_candidate=None,
            issues=tuple(issues),
            source_sheet_name=row.sheet_name,
            source_row_number=row.row_number,
            raw_row_snapshot=row.raw_row_snapshot,
        )

    def _activate(self, candidate: PurchaseRequestCandidate) -> None:
        self._state = GroupState.ACTIVE_VALID_REQUEST
        self._current_request = candidate
        self._invalid_start_row_number = None
        self._invalid_error_codes = ()

    def _ensure_same_sheet(self, sheet_name: str) -> None:
        if self._sheet_name and sheet_name != self._sheet_name:
            raise ValueError("One grouper cannot combine rows from different worksheets")

    def _result(self, row: ConvertedRow, decision: GroupingDecision) -> GroupingResult:
        request_start_row_number = (
            self._current_request.source_start_row_number
            if self._current_request is not None
            else None
        )
        return GroupingResult(
            decision,
            None,
            request_start_row_number,
            None,
            (),
            row.sheet_name,
            row.row_number,
            row.raw_row_snapshot,
        )


def _issue(code: IssueCode, field_name: str) -> ImportIssue:
    return ImportIssue(code, field_name, code.value.replace("_", " "))
