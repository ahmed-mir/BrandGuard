from django.urls import path

from . import views

app_name = "content_reviews"

urlpatterns = [
    path("", views.submission_list, name="list"),
    path("new/", views.submission_create, name="create"),
    path("<uuid:submission_id>/", views.submission_detail, name="detail"),
    path("<uuid:submission_id>/versions/<int:version_number>/", views.version_detail, name="version_detail"),
    path(
        "<uuid:submission_id>/versions/<int:version_number>/run/",
        views.review_start,
        name="run",
    ),
    path(
        "<uuid:submission_id>/versions/<int:version_number>/runs/<uuid:review_id>/",
        views.review_detail,
        name="review_detail",
    ),
]
