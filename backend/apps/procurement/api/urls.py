from django.urls import URLPattern, path

from apps.procurement.api import views

urlpatterns: list[URLPattern] = [
    path("", views.list_purchase_requests, name="list-purchase-requests"),
    path("summary/", views.get_purchase_request_summary, name="purchase-request-summary"),
    path("<int:purchase_request_id>/", views.get_purchase_request, name="get-purchase-request"),
    path(
        "<int:purchase_request_id>/items/",
        views.list_purchase_request_items,
        name="list-purchase-request-items",
    ),
]
