from django.urls import path

from . import views

app_name = "brand_sources"

urlpatterns = [
    path("", views.source_list, name="list"),
    path("new/", views.source_create, name="create"),
    path("import/", views.document_import, name="import"),
    path("<uuid:source_id>/", views.source_detail, name="detail"),
    path("<uuid:source_id>/versions/new/", views.source_version_create, name="version_create"),
    path("<uuid:source_id>/versions/<int:version_number>/", views.source_version_detail, name="version_detail"),
    path("<uuid:source_id>/versions/<int:version_number>/edit/", views.source_version_edit, name="version_edit"),
    path("<uuid:source_id>/versions/<int:version_number>/approve/", views.source_version_approve, name="approve"),
    path("<uuid:source_id>/versions/<int:version_number>/confirm-import/", views.source_version_confirm_import, name="confirm_import"),
    path("<uuid:source_id>/versions/<int:version_number>/rules/new/", views.rule_create, name="rule_create"),
    path("<uuid:source_id>/versions/<int:version_number>/rules/<uuid:rule_id>/edit/", views.rule_edit, name="rule_edit"),
    path("<uuid:source_id>/versions/<int:version_number>/rules/<uuid:rule_id>/delete/", views.rule_delete, name="rule_delete"),
    path("<uuid:source_id>/versions/<int:version_number>/facts/new/", views.fact_create, name="fact_create"),
    path("<uuid:source_id>/versions/<int:version_number>/facts/<uuid:fact_id>/edit/", views.fact_edit, name="fact_edit"),
    path("<uuid:source_id>/versions/<int:version_number>/facts/<uuid:fact_id>/delete/", views.fact_delete, name="fact_delete"),
]
