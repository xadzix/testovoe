# Асинхронный импорт заявок из XLSX

Django-приложение загружает XLSX-файл с заявками на закупку, обрабатывает его в Celery
Worker и сохраняет заявки, товарные позиции и ошибки строк в PostgreSQL. Сохранённые данные
доступны через HTTP API, включая агрегированную статистику.

## Технологии

- Python 3.12;
- Django 5.2;
- PostgreSQL 17;
- Celery 5.6 и Redis 7.4;
- openpyxl для потокового чтения XLSX;
- Pytest, pytest-django и Ruff;
- Docker Compose для локального запуска.


## Структура проекта

```text
backend/
├── config/                         # настройки Django, маршруты URL, приложение Celery
├── apps/
│   ├── data_imports/               # жизненный цикл импорта и XLSX-инфраструктура
│   │   ├── api/                    # загрузка файла, статус и ошибки строк
│   │   ├── readers/xlsx.py         # потоковое чтение XLSX
│   │   ├── services/               # создание и выполнение ImportJob
│   │   ├── contracts.py            # DTO исходных строк и ячеек
│   │   ├── header_mapping.py       # сопоставление заголовков
│   │   ├── models.py               # ImportJob и ImportRowIssue
│   │   └── tasks.py                # тонкие задачи Celery
│   └── procurement/                # предметная область заявок на закупку
│       ├── api/                    # заявки, позиции и агрегаты
│       ├── importing/              # преобразование, валидация, группировка, запись
│       └── models.py               # PurchaseRequest и PurchaseRequestItem
└── tests/
```

`data_imports` отвечает за хранение файла, состояние задания, чтение XLSX и оркестрацию.
`procurement` содержит правила заявок и товарных позиций. HTTP-представления и задача Celery не
содержат бизнес-логику импорта.

## Конвейер импорта

```text
XlsxSourceReader
    → HeaderMapper
    → PurchaseRequestRowConverter
    → validators
    → PurchaseRequestRowGrouper
    → PurchaseRequestBatchWriter
```

1. API потоково сохраняет файл в общем Docker volume, вычисляет SHA-256 и создаёт
   `ImportJob`. В Redis передаётся только идентификатор задания.
2. `XlsxSourceReader` открывает книгу в режиме `read_only` и выдаёт строки с координатами,
   исходными значениями, типами и форматами ячеек.
3. `HeaderMapper` сопоставляет девять заголовков с каноническими полями независимо от
   порядка колонок и поддерживает известные алиасы.
4. Converter нормализует даты, строки, `Decimal`, валюту, срок поставки и классификацию
   `original/analogue` без обращения к БД.
5. Validators проверяют обязательные значения, длины и точность чисел.
6. Grouper объединяет последовательные строки в `PurchaseRequest`. Пустые дата и заказчик
   наследуются из текущего контекста; поля товарной позиции не наследуются.
7. Batch Writer атомарно сохраняет заявки, позиции, ошибки, счётчики и checkpoint через
   Django ORM и `bulk_create`.

Повторное выполнение защищено ограничениями PostgreSQL по координатам исходных строк,
checkpoint, `execution_token`, арендой задания и транзакциями на уровне пакета.

## Локальный запуск

Создать локальный файл окружения:

```bash
cp .env.example .env
```

Собрать и запустить сервисы:

```bash
docker compose up -d --build
docker compose exec web python manage.py migrate --noinput
docker compose exec web python manage.py check
docker compose ps
```

Проверить API:

```bash
curl http://localhost:8000/api/v1/purchase-requests/
```

Остановить сервисы:

```bash
docker compose down
```

## API

После загрузки скопируйте UUID и при необходимости идентификатор заявки в переменные:

```bash
IMPORT_JOB_ID="00000000-0000-0000-0000-000000000000"
PURCHASE_REQUEST_ID="1"
```

### Загрузить XLSX

```bash
curl -X POST http://localhost:8000/api/v1/import-jobs/ \
  -F "file=@sample_data/test_requests.xlsx.XLSX" \
  -F "sheet_name=request"
```

Успешный ответ имеет HTTP 202. Поле `sheet_name` необязательно; без него импортируется
первый лист книги.

### Получить статус импорта

```bash
curl "http://localhost:8000/api/v1/import-jobs/$IMPORT_JOB_ID/"
```

Статус принимает значения `queued`, `running`, `completed`, `completed_with_issues` или
`failed`. Ответ содержит счётчики обработанных строк, заявок, позиций и текущий checkpoint.

### Получить ошибки строк

```bash
curl "http://localhost:8000/api/v1/import-jobs/$IMPORT_JOB_ID/row-issues/?page=1&page_size=50"
```

### Получить список заявок

```bash
curl "http://localhost:8000/api/v1/purchase-requests/?page=1&page_size=50"
```

Доступны фильтры `import_job_id`, `request_date` и `customer_name`:

```bash
curl "http://localhost:8000/api/v1/purchase-requests/?import_job_id=$IMPORT_JOB_ID&request_date=2025-01-01&customer_name=Тест"
```

API возвращает только заявки из импортов со статусом `completed` или
`completed_with_issues`.

### Получить заявку и её позиции

```bash
curl "http://localhost:8000/api/v1/purchase-requests/$PURCHASE_REQUEST_ID/"
curl "http://localhost:8000/api/v1/purchase-requests/$PURCHASE_REQUEST_ID/items/?page=1&page_size=50"
```

Позиции вынесены в отдельный маршрут с пагинацией. Значения `Decimal` возвращаются
строками, чтобы JSON-сериализация не меняла точность.

### Получить агрегированную статистику

```bash
curl http://localhost:8000/api/v1/purchase-requests/summary/
curl "http://localhost:8000/api/v1/purchase-requests/summary/?import_job_id=$IMPORT_JOB_ID"
```

Ответ содержит количество заявок и позиций, суммарное количество товаров, стоимость по
каждой валюте и пять каталожных номеров с максимальным суммарным количеством. Значения
разных валют не складываются.

## Образцы XLSX

Каталог `sample_data/` содержит файлы для тестирования:

- `request CAT.xlsx` — исходный файл на 2992 строки, лист `request`;
- `test_requests.xlsx.XLSX` — компактный файл на 43 строки, лист `request`.

Загрузить файлы из Git Bash:

```bash
curl -X POST http://localhost:8000/api/v1/import-jobs/ \
  -F "file=@sample_data/request CAT.xlsx" \
  -F "sheet_name=request"

curl -X POST http://localhost:8000/api/v1/import-jobs/ \
  -F "file=@sample_data/test_requests.xlsx.XLSX" \
  -F "sheet_name=request"
```

Команды не выводят содержимое XLSX. Для проверки прогресса используйте маршрут статуса
с UUID из ответа.

## Проверки

```bash
docker compose exec web python -m pytest -q
docker compose exec web ruff check .
docker compose exec web ruff format --check .
docker compose exec web python manage.py makemigrations --check --dry-run
```
