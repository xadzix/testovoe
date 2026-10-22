from uuid import UUID

from celery import shared_task
from celery.app.task import Task
from django.conf import settings
from django.db import OperationalError

from apps.data_imports.services.import_execution import (
    ImportAlreadyRunningError,
    ImportExecutionService,
)


@shared_task(name="data_imports.diagnostic_ping")
def diagnostic_ping() -> str:
    return "pong"


@shared_task(
    bind=True,
    name="data_imports.import_purchase_requests",
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(OperationalError,),
    retry_kwargs={"max_retries": 5},
    retry_backoff=True,
    retry_jitter=True,
    max_retries=None,
)
def import_purchase_requests(task: Task, import_job_id: str) -> None:
    try:
        ImportExecutionService().execute(UUID(import_job_id))
    except ImportAlreadyRunningError as error:
        countdown = int(settings.IMPORT_LEASE_TIMEOUT_SECONDS) + 1
        raise task.retry(exc=error, countdown=countdown) from error
