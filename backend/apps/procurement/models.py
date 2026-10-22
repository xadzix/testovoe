from django.db import models

from apps.data_imports.models import ImportJob


class PartClassification(models.TextChoices):
    ORIGINAL = "original", "Original"
    ANALOGUE = "analogue", "Analogue"


class PurchaseRequest(models.Model):
    import_job = models.ForeignKey(
        ImportJob,
        on_delete=models.PROTECT,
        related_name="purchase_requests",
    )
    source_sheet_name = models.CharField(max_length=255)
    source_start_row_number = models.PositiveBigIntegerField()
    request_date = models.DateField()
    request_date_raw = models.TextField(blank=True)
    customer_name = models.CharField(max_length=500)
    customer_name_raw = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=~models.Q(source_sheet_name=""),
                name="nonempty_request_sheet_name",
            ),
            models.CheckConstraint(
                condition=models.Q(source_start_row_number__gt=0),
                name="positive_request_source_start_row",
            ),
            models.CheckConstraint(
                condition=~models.Q(customer_name=""),
                name="nonempty_request_customer_name",
            ),
            models.UniqueConstraint(
                fields=("import_job", "source_sheet_name", "source_start_row_number"),
                name="uniq_request_source_origin",
            ),
        )
        indexes = (models.Index(fields=("request_date",), name="request_date_idx"),)

    def __str__(self) -> str:
        return f"{self.import_job_id}:{self.source_sheet_name}:{self.source_start_row_number}"


class PurchaseRequestItem(models.Model):
    purchase_request = models.ForeignKey(
        PurchaseRequest,
        on_delete=models.CASCADE,
        related_name="items",
    )
    source_row_number = models.PositiveBigIntegerField()
    catalog_number = models.CharField(max_length=255, blank=True)
    catalog_number_search_key = models.CharField(max_length=255, blank=True)
    product_name = models.TextField(blank=True)
    brand_name = models.CharField(max_length=255, blank=True)
    quantity = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    quantity_raw = models.TextField(blank=True)
    price_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    price_raw = models.TextField(blank=True)
    currency_code = models.CharField(max_length=3, blank=True)
    currency_raw = models.TextField(blank=True)
    delivery_time = models.TextField(blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    delivery_min_days = models.PositiveIntegerField(null=True, blank=True)
    delivery_max_days = models.PositiveIntegerField(null=True, blank=True)
    part_classification = models.CharField(
        max_length=16,
        choices=PartClassification,
        blank=True,
    )
    part_classification_raw = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=models.Q(source_row_number__gt=0),
                name="positive_item_source_row",
            ),
            models.CheckConstraint(
                condition=~models.Q(catalog_number="") | ~models.Q(product_name=""),
                name="item_has_catalog_or_product_name",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__isnull=True) | models.Q(quantity__gt=0),
                name="null_or_positive_item_quantity",
            ),
            models.CheckConstraint(
                condition=models.Q(price_amount__isnull=True) | models.Q(price_amount__gte=0),
                name="null_or_nonnegative_item_price",
            ),
            models.CheckConstraint(
                condition=models.Q(currency_code="") | models.Q(currency_code__regex=r"^[A-Z]{3}$"),
                name="blank_or_valid_item_currency",
            ),
            models.CheckConstraint(
                condition=models.Q(part_classification="")
                | models.Q(part_classification__in=PartClassification.values),
                name="blank_or_valid_part_classification",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        delivery_date__isnull=True,
                        delivery_min_days__isnull=True,
                        delivery_max_days__isnull=True,
                    )
                    | models.Q(
                        delivery_date__isnull=False,
                        delivery_min_days__isnull=True,
                        delivery_max_days__isnull=True,
                    )
                    | models.Q(
                        delivery_date__isnull=True,
                        delivery_min_days__isnull=False,
                        delivery_max_days__isnull=False,
                        delivery_min_days__lte=models.F("delivery_max_days"),
                    )
                ),
                name="consistent_item_delivery_normalization",
            ),
            models.UniqueConstraint(
                fields=("purchase_request", "source_row_number"),
                name="uniq_item_source_origin",
            ),
        )
        indexes = (
            models.Index(fields=("catalog_number_search_key",), name="item_catalog_key_idx"),
        )

    def __str__(self) -> str:
        return f"{self.purchase_request_id}:{self.source_row_number}"
