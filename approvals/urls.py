from django.urls import path

from . import views

app_name = "approvals"

urlpatterns = [
    path("", views.approval_inbox, name="inbox"),
    path("<uuid:request_id>/", views.approval_detail, name="detail"),
]
