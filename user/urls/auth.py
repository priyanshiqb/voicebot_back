from django.urls import path
from user.views.auth import (
    LoginAPIView,
    LogoutAPIView,
    TokenRefreshAPIView,
)

urlpatterns = [

    path(
        "login/",
        LoginAPIView.as_view(),
        name="login"
    ),

    path(
        "logout/",
        LogoutAPIView.as_view(),
        name="logout"
    ),

    path(
        "refresh/",
        TokenRefreshAPIView.as_view(),
        name="token_refresh"
    ),
]