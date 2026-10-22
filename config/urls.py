from django.urls import include, path

from core import views as core_views

urlpatterns = [
    path("", core_views.landing, name="landing"),
    path("health/", core_views.health, name="health"),
    path("accounts/", include("accounts.urls")),
    path("app/", include("workspaces.urls")),
]
