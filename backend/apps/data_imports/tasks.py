from celery import shared_task


@shared_task(name="data_imports.diagnostic_ping")
def diagnostic_ping() -> str:
    return "pong"
