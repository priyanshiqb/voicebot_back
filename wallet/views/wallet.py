from drf_spectacular.utils import extend_schema
from rest_framework import status

from tenant.common.views.tenant_base_api_view import TenantBaseAPIView
from wallet.models.credit_rate import CreditRate
from wallet.serializers import (
    CreditQuoteRequestSerializer,
    CreditQuoteSerializer,
    WalletBalanceSerializer,
    WalletRateCardSerializer,
)
from wallet.services import wallet as wallet_service
from wallet.services.exceptions import RateNotConfigured


@extend_schema(
    tags=["Billing - Wallet"],
    summary="Current credit balance",
    description=(
        "The tenant's balance, split into plan-granted and purchased credits, plus "
        "whether spending is currently possible."
    ),
    responses={200: WalletBalanceSerializer},
)
class WalletBalanceView(TenantBaseAPIView):
    """One row read. Safe to poll from a dashboard."""

    serializer_class = WalletBalanceSerializer

    def get(self, request):
        wallet = wallet_service.for_tenant(self.get_tenant())
        return self.success(
            data=WalletBalanceSerializer(wallet).data,
            message="Wallet balance retrieved",
        )


@extend_schema(
    tags=["Billing - Wallet"],
    summary="Rate card in force",
    description=(
        "What each call tier costs this tenant per minute. Read from the rate card "
        "stamped on the wallet when their plan was activated — not from the live "
        "catalogue — so it reflects the terms they actually bought."
    ),
    responses={200: WalletRateCardSerializer},
)
class WalletRateCardView(TenantBaseAPIView):
    serializer_class = WalletRateCardSerializer

    def get(self, request):
        wallet = wallet_service.for_tenant(self.get_tenant())
        snapshot = wallet.rate_snapshot or {}
        labels = dict(CreditRate.Operation.choices)

        operations = [
            {
                "operation": operation,
                "label": labels.get(operation, operation),
                "unit": entry.get("unit"),
                "credits_per_unit": entry.get("credits_per_unit"),
                "rounding": entry.get("rounding"),
                "minimum_units": entry.get("minimum_units"),
            }
            for operation, entry in sorted((snapshot.get("operations") or {}).items())
        ]

        return self.success(
            data={
                "plan_code": snapshot.get("plan_code"),
                "stamped_at": snapshot.get("stamped_at"),
                "operations": operations,
            },
            message=(
                "Rate card retrieved"
                if operations
                else "No rate card is stamped on this wallet yet"
            ),
        )


@extend_schema(
    tags=["Billing - Wallet"],
    summary="Quote the cost of an operation",
    description=(
        "What a given quantity would cost, and whether the balance covers it. Purely "
        "a calculation — nothing is reserved, deducted or recorded."
    ),
    request=CreditQuoteRequestSerializer,
    responses={200: CreditQuoteSerializer},
)
class CreditQuoteView(TenantBaseAPIView):
    """Shares one computation with the consumption path.

    A quote and the charge that follows it come from the same ``rate_quantity`` call,
    so the number shown to a tenant is the number they are charged.
    """

    serializer_class = CreditQuoteRequestSerializer

    def post(self, request):
        payload = CreditQuoteRequestSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(
                message="Validation failed",
                errors=payload.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        tenant = self.get_tenant()
        try:
            result = wallet_service.quote(
                tenant,
                operation=payload.validated_data["operation"],
                quantity=payload.validated_data["quantity"],
            )
        except RateNotConfigured as exc:
            # Not a 500: the request was well-formed, the plan simply has no price for
            # it. 409 says "the server's state cannot answer this", which is exactly
            # the case and is actionable by whoever configures plans.
            return self.error(
                message=str(exc),
                errors=["RATE_NOT_CONFIGURED"],
                status_code=status.HTTP_409_CONFLICT,
            )

        wallet = wallet_service.for_tenant(tenant)
        return self.success(
            data={
                "operation": result.operation,
                "credits": result.credits,
                "units": result.units,
                "raw_quantity": result.raw_quantity,
                "unit_type": result.unit_type,
                "rate_applied": result.rate_applied,
                "affordable": wallet.balance >= result.credits and not wallet.is_frozen,
                "balance": wallet.balance,
            },
            message="Quote calculated",
        )
