from django.urls import URLPattern, path

from apps.data_imports.api import views

urlpatterns: list[URLPattern] = [
    path("", views.create_import_job, name="create-import-job"),
    path("<uuid:import_job_id>/", views.get_import_job, name="get-import-job"),
    path(
        "<uuid:import_job_id>/row-issues/",
        views.list_import_issues,
        name="list-import-row-issues",
    ),
]
