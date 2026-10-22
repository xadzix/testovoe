from uuid import UUID

from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.data_imports.models import ImportJob, ImportRowIssue
from apps.data_imports.services.import_jobs import (
    ImportJobService,
    ImportPublicationError,
    InvalidUploadError,
    UploadTooLargeError,
)


@csrf_exempt
@require_POST
def create_import_job(request: HttpRequest) -> JsonResponse:
    uploaded_file = request.FILES.get("file")
    if not isinstance(uploaded_file, UploadedFile):
        return JsonResponse({"error": "file_required"}, status=400)
    try:
        import_job = ImportJobService().create(
            uploaded_file,
            sheet_name=request.POST.get("sheet_name", "").strip(),
        )
    except UploadTooLargeError as error:
        return JsonResponse({"error": "upload_too_large", "message": str(error)}, status=413)
    except InvalidUploadError as error:
        return JsonResponse({"error": "invalid_upload", "message": str(error)}, status=400)
    except ImportPublicationError as error:
        return JsonResponse(
            {"error": "import_service_unavailable", "message": str(error)},
            status=503,
        )
    return JsonResponse(_job_payload(import_job), status=202)


@require_GET
def get_import_job(_request: HttpRequest, import_job_id: UUID) -> JsonResponse:
    import_job = get_object_or_404(ImportJob, pk=import_job_id)
    return JsonResponse(_job_payload(import_job))


@require_GET
def list_import_issues(request: HttpRequest, import_job_id: UUID) -> JsonResponse:
    get_object_or_404(ImportJob, pk=import_job_id)
    try:
        page = max(int(request.GET.get("page", "1")), 1)
        page_size = min(max(int(request.GET.get("page_size", "50")), 1), 100)
    except ValueError:
        return JsonResponse({"error": "invalid_pagination"}, status=400)

    issues = ImportRowIssue.objects.filter(import_job_id=import_job_id).order_by(
        "source_row_number"
    )
    total = issues.count()
    offset = (page - 1) * page_size
    results = [
        {
            "source_sheet_name": issue.source_sheet_name,
            "source_row_number": issue.source_row_number,
            "severity": issue.severity,
            "issue_codes": issue.issue_codes,
            "messages": issue.messages,
            "raw_row_snapshot": issue.raw_row_snapshot,
        }
        for issue in issues[offset : offset + page_size]
    ]
    return JsonResponse({"count": total, "page": page, "page_size": page_size, "results": results})


def _job_payload(import_job: ImportJob) -> dict[str, object]:
    return {
        "id": str(import_job.id),
        "status": import_job.status,
        "original_filename": import_job.original_filename,
        "processed_row_count": import_job.processed_row_count,
        "skipped_row_count": import_job.skipped_row_count,
        "invalid_row_count": import_job.invalid_row_count,
        "imported_request_count": import_job.imported_request_count,
        "imported_item_count": import_job.imported_item_count,
        "checkpoint_row_number": import_job.checkpoint_row_number,
        "failure_code": import_job.failure_code,
        "failure_message": import_job.failure_message,
        "created_at": import_job.created_at.isoformat(),
        "started_at": import_job.started_at.isoformat() if import_job.started_at else None,
        "finished_at": import_job.finished_at.isoformat() if import_job.finished_at else None,
        "issues_url": f"/api/v1/import-jobs/{import_job.id}/row-issues/",
    }
