from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path

urlpatterns: list[URLPattern | URLResolver] = [
    path("admin/", admin.site.urls),
    path("api/v1/import-jobs/", include("apps.data_imports.api.urls")),
    path("api/v1/purchase-requests/", include("apps.procurement.api.urls")),
]
