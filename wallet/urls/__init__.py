"""Billing URLs.

Mounted at `/api/v1/billing/` — its own prefix rather than under `onboard` (which is
the signup flow, authenticated as a TempUser) or `tenant` (identity and settings).
Billing is neither, and the subscription endpoints will join it here.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from wallet.views import (
    CreditLedgerViewSet,
    CreditQuoteView,
    WalletBalanceView,
    WalletRateCardView,
)

app_name = "billing"

router = DefaultRouter()
router.register(r"wallet/ledger", CreditLedgerViewSet, basename="credit-ledger")

urlpatterns = [
    path("wallet/", WalletBalanceView.as_view(), name="wallet-balance"),
    path("wallet/rates/", WalletRateCardView.as_view(), name="wallet-rates"),
    path("wallet/quote/", CreditQuoteView.as_view(), name="wallet-quote"),
    path("", include(router.urls)),
]
