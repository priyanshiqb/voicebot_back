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
)

urlpatterns = [
    path('register-pending/', TempUserRegisterView.as_view(), name='register-pending'),
    path('verify-otp/', VerifyOTPView.as_view(), name='verify-otp'),
    path('resend-otp/', ResendOTPView.as_view(), name='resend-otp'),
    path('resume-session/', ResumeSessionView.as_view(), name='resume-session'),
    path('update-plan/', UpdatePlanView.as_view(), name='update-plan'),
    path('create-order-just-in-time/', CreateOrderJustInTimeView.as_view(), name='create-order-just-in-time'),
    path('verify/', VerifyPaymentView.as_view(), name='verify'),
    path('cancel-order/', CancelOrderView.as_view(), name='cancel-order'),
    path('webhook/', RazorpayWebhookView.as_view(), name='webhook'),
]

