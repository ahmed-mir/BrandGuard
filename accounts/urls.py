from django.urls import path

from .views import BrandGuardLoginView, PostOnlyLogoutView

app_name = "accounts"

urlpatterns = [
    path("login/", BrandGuardLoginView.as_view(), name="login"),
    path("logout/", PostOnlyLogoutView.as_view(), name="logout"),
]
