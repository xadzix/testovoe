import uuid

from django.db import models


class ImportJobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    COMPLETED_WITH_ISSUES = "completed_with_issues", "Completed with issues"
    FAILED = "failed", "Failed"


class ImportEntityType(models.TextChoices):
    PURCHASE_REQUESTS = "purchase_requests", "Purchase requests"


class SourceFormat(models.TextChoices):
    XLSX = "xlsx", "XLSX"


class ImportIssueSeverity(models.TextChoices):
    WARNING = "warning", "Warning"
    ERROR = "error", "Error"


class ImportJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    status = models.CharField(
        max_length=32,
        choices=ImportJobStatus,
        default=ImportJobStatus.QUEUED,
    )
    entity_type = models.CharField(
        max_length=32,
        choices=ImportEntityType,
        default=ImportEntityType.PURCHASE_REQUESTS,
    )
    source_format = models.CharField(
        max_length=16,
        choices=SourceFormat,
        default=SourceFormat.XLSX,
    )
    original_filename = models.CharField(max_length=255)
    storage_key = models.CharField(max_length=1024)
    source_byte_size = models.PositiveBigIntegerField()
    source_sha256 = models.CharField(max_length=64)
    security_scope = models.CharField(
        max_length=255,
        help_text="Must be derived from trusted server-side context, never client input.",
    )
    idempotency_key = models.CharField(max_length=255, blank=True)
    configuration_snapshot = models.JSONField(default=dict)
    configuration_hash = models.CharField(max_length=64)
    importer_version = models.CharField(max_length=64)
    selected_sheet_name = models.CharField(max_length=255, blank=True)

    processed_row_count = models.PositiveBigIntegerField(default=0)
    skipped_row_count = models.PositiveBigIntegerField(default=0)
    invalid_row_count = models.PositiveBigIntegerField(default=0)
    imported_request_count = models.PositiveBigIntegerField(default=0)
    imported_item_count = models.PositiveBigIntegerField(default=0)

    checkpoint_row_number = models.PositiveBigIntegerField(default=0)
    checkpoint_state = models.JSONField(default=dict)
    attempt_count = models.PositiveIntegerField(default=0)
    execution_token = models.UUIDField(null=True, blank=True, editable=False)
    celery_task_id = models.CharField(max_length=255, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=100, blank=True)
    failure_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=models.Q(status__in=ImportJobStatus.values),
                name="valid_import_job_status",
            ),
            models.CheckConstraint(
                condition=models.Q(entity_type__in=ImportEntityType.values),
                name="valid_import_entity_type",
            ),
            models.CheckConstraint(
                condition=models.Q(source_format__in=SourceFormat.values),
                name="valid_import_source_format",
            ),
            models.CheckConstraint(
                condition=~models.Q(original_filename=""),
                name="nonempty_import_original_filename",
            ),
            models.CheckConstraint(
                condition=~models.Q(storage_key=""),
                name="nonempty_import_storage_key",
            ),
            models.CheckConstraint(
                condition=models.Q(source_sha256__regex=r"^[0-9a-f]{64}$"),
                name="valid_import_source_sha256",
            ),
            models.CheckConstraint(
                condition=~models.Q(security_scope=""),
                name="nonempty_import_security_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(configuration_hash__regex=r"^[0-9a-f]{64}$"),
                name="valid_import_configuration_hash",
            ),
            models.CheckConstraint(
                condition=~models.Q(importer_version=""),
                name="nonempty_importer_version",
            ),
            models.UniqueConstraint(
                fields=("security_scope", "idempotency_key"),
                condition=~models.Q(idempotency_key=""),
                name="uniq_import_scope_idempotency_key",
            ),
        )
        indexes = (
            models.Index(
                fields=("security_scope", "source_sha256", "configuration_hash"),
                name="import_job_source_hash_idx",
            ),
        )

    def __str__(self) -> str:
        return f"{self.entity_type}:{self.id}"


class ImportRowIssue(models.Model):
    import_job = models.ForeignKey(
        ImportJob,
        on_delete=models.CASCADE,
        related_name="row_issues",
    )
    source_sheet_name = models.CharField(max_length=255)
    source_row_number = models.PositiveBigIntegerField()
    severity = models.CharField(max_length=16, choices=ImportIssueSeverity)
    issue_codes = models.JSONField(default=list)
    messages = models.JSONField(default=list)
    raw_row_snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=models.Q(severity__in=ImportIssueSeverity.values),
                name="valid_import_issue_severity",
            ),
            models.CheckConstraint(
                condition=~models.Q(source_sheet_name=""),
                name="nonempty_issue_sheet_name",
            ),
            models.CheckConstraint(
                condition=models.Q(source_row_number__gt=0),
                name="positive_issue_source_row",
            ),
            models.UniqueConstraint(
                fields=("import_job", "source_sheet_name", "source_row_number"),
                name="uniq_issue_source_origin",
            ),
        )

    def __str__(self) -> str:
        return f"{self.import_job_id}:{self.source_sheet_name}:{self.source_row_number}"
