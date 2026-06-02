from django.urls import path
from user.views.auth import (
    LoginAPIView,
    LogoutAPIView,
    TokenRefreshAPIView,
    UserForgotPasswordAPIView,
    UserForgotPasswordVerifyOTPAPIView,
    UserResetPasswordAPIView,
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

    path(
        "forgot-password/",
        UserForgotPasswordAPIView.as_view(),
        name="user-forgot-password"
    ),

    path(
        "forgot-password/verify/",
        UserForgotPasswordVerifyOTPAPIView.as_view(),
        name="user-forgot-password-verify"
    ),

    path(
        "reset-password/",
        UserResetPasswordAPIView.as_view(),
        name="user-reset-password"
    ),
]