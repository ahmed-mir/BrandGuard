from django.urls import path

from . import views

app_name = "ai_analysis"

urlpatterns = [
    path("analyze/", views.analyze_version, name="analyze"),
]
