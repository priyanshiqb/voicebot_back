"""Purchase and subscription endpoints, mounted with the wallet under /api/v1/billing/.

Deliberately not under /api/v1/onboard/: that prefix is the signup flow and is
authenticated as a TempUser. These are for tenants who already have an account.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from onboard.views.billing import (
    AddonOrderView,
    AddonQuoteView,
    CancelScheduledChangeView,
    EligiblePlansView,
    ChangePlanView,
    CurrentSubscriptionView,
    PreviewPlanChangeView,
    RenewSubscriptionView,
    TopUpOrderView,
    TopUpQuoteView,
    TransactionViewSet,
    VerifyPaymentView,
)

router = DefaultRouter()
# Looked up by razorpay_order_id -- the identifier the rest of the payment flow
# speaks, including the 409 that sends a tenant here.
router.register(r"transactions", TransactionViewSet, basename="transaction")

urlpatterns = [
    path("subscription/", CurrentSubscriptionView.as_view(), name="subscription-current"),
    path("subscription/eligible-plans/", EligiblePlansView.as_view(),
         name="subscription-eligible-plans"),
    path("subscription/renew/", RenewSubscriptionView.as_view(), name="subscription-renew"),
    path("subscription/preview-change/", PreviewPlanChangeView.as_view(),
         name="subscription-preview-change"),
    path("subscription/change-plan/", ChangePlanView.as_view(), name="subscription-change-plan"),
    path("subscription/scheduled-change/", CancelScheduledChangeView.as_view(),
         name="subscription-cancel-scheduled"),

    # Called by the browser when Checkout succeeds, for every purchase type.
    # Races the webhook; whichever lands first applies the payment.
    path("verify-payment/", VerifyPaymentView.as_view(), name="verify-payment"),

    path("top-up/quote/", TopUpQuoteView.as_view(), name="top-up-quote"),
    path("top-up/", TopUpOrderView.as_view(), name="top-up"),
    path("add-ons/quote/", AddonQuoteView.as_view(), name="addon-quote"),
    path("add-ons/", AddonOrderView.as_view(), name="addon"),

    path("", include(router.urls)),
]
