FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend

WORKDIR /app

COPY requirements /app/requirements
RUN python -m pip install --no-cache-dir -r /app/requirements/dev.txt

COPY pyproject.toml /app/pyproject.toml
COPY backend /app/backend

WORKDIR /app/backend

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

