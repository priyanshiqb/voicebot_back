from django.urls import path
from onboard.views.onboarding import (
    TempUserRegisterView,
    VerifyOTPView,
    UpdatePlanView,
    CreateOrderJustInTimeView,
    VerifyPaymentView,
    CancelOrderView,
    RazorpayWebhookView,
    ResendOTPView,
    ResumeSessionView,
    ResumeSessionWithTokenView,
    ChangeEmailView,
    ForgotPasswordView,
    ForgotPasswordVerifyOTPView,
    ForgotPasswordResendOTPView,
    ResetPasswordView,
    CheckEmailView,
)

urlpatterns = [
    path('check-email/', CheckEmailView.as_view(), name='check-email'),
    path('register-pending/', TempUserRegisterView.as_view(), name='register-pending'),
    path('verify-otp/', VerifyOTPView.as_view(), name='verify-otp'),
    path('change-email/', ChangeEmailView.as_view(), name='change-email'),
    path('resend-otp/', ResendOTPView.as_view(), name='resend-otp'),
    path('resume-session/', ResumeSessionView.as_view(), name='resume-session'),
    path('resume-session-token/', ResumeSessionWithTokenView.as_view(), name='resume-session-token'),
    path('forgot-password/', ForgotPasswordView.as_view(), name='forgot-password'),
    path('forgot-password/verify/', ForgotPasswordVerifyOTPView.as_view(), name='forgot-password-verify'),
    path('forgot-password/resend/', ForgotPasswordResendOTPView.as_view(), name='forgot-password-resend'),
    path('reset-password/', ResetPasswordView.as_view(), name='reset-password'),
    path('update-plan/', UpdatePlanView.as_view(), name='update-plan'),
    path('create-order-just-in-time/', CreateOrderJustInTimeView.as_view(), name='create-order-just-in-time'),
    path('verify/', VerifyPaymentView.as_view(), name='verify'),
    path('cancel-order/', CancelOrderView.as_view(), name='cancel-order'),
    path('webhook/', RazorpayWebhookView.as_view(), name='webhook'),
]

