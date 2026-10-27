from django.urls import include, path

from . import export_views, views

app_name = "content_reviews"

urlpatterns = [
    path("", views.submission_list, name="list"),
    path("new/", views.submission_create, name="create"),
    path("<uuid:submission_id>/export/txt/", export_views.export_approved_text, name="export_txt"),
    path("<uuid:submission_id>/export/pdf/", export_views.export_approved_pdf, name="export_pdf"),
    path("<uuid:submission_id>/", views.submission_detail, name="detail"),
    path("<uuid:submission_id>/revise/", views.revise_submission, name="revise"),
    path("<uuid:submission_id>/submit/", views.submit_submission, name="submit"),
    path("<uuid:submission_id>/compare/", views.compare_versions, name="compare"),
    path("<uuid:submission_id>/versions/<int:version_number>/", views.version_detail, name="version_detail"),
    path("<uuid:submission_id>/versions/<int:version_number>/comments/", views.post_version_comment, name="version_comment"),
    path("<uuid:submission_id>/versions/<int:version_number>/findings/<uuid:finding_id>/comments/", views.post_finding_comment, name="finding_comment"),
    path("<uuid:submission_id>/versions/<int:version_number>/findings/<uuid:finding_id>/resolution/", views.update_finding_resolution, name="finding_resolution"),
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
    path(
        "<uuid:submission_id>/versions/<int:version_number>/ai/",
        include(("ai_analysis.urls", "ai_analysis"), namespace="ai_analysis"),
    ),
]
