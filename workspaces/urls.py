from django.urls import path

from accounts.views import profile_settings
from . import views

app_name = "app"

urlpatterns = [
    path("", views.home, name="home"),
    path("members/", views.members, name="members"),
    path("settings/profile/", profile_settings, name="profile"),
]
