from django.urls import include, path

from accounts.views import profile_settings
from . import views

app_name = "app"

urlpatterns = [
    path("", views.home, name="home"),
    path("members/", views.members, name="members"),
    path("settings/profile/", profile_settings, name="profile"),
    path(
        "brand-sources/",
        include(("brand_sources.urls", "brand_sources"), namespace="brand_sources"),
    ),
    path(
        "reviews/",
        include(("content_reviews.urls", "content_reviews"), namespace="content_reviews"),
    ),
    path(
        "approvals/",
        include(("approvals.urls", "approvals"), namespace="approvals"),
    ),
]
