"""The 360 envelope.

A plain function rather than a ``Serializer`` over a dict-of-dicts: the payload is an
assembly of eleven independent blocks, and a serializer whose every field is a
``SerializerMethodField`` returning another serializer's ``.data`` is a longer way to
write the same dictionary with less control over the degraded cases.
"""

from ops.serializers.tenant.blocks import (
    EMPTY_WALLET,
    LedgerRowSerializer,
    OwnerSerializer,
    PaymentRowSerializer,
    SubscriptionBlockSerializer,
    TenantProfileSerializer,
    WalletBlockSerializer,
)


def render(assembled):
    """Turn ``ops.services.tenant.detail.assemble()`` output into the payload.

    Every block is rendered independently so a tenant with nothing configured still
    produces a complete, readable response.
    """
    settings_row = assembled["settings"]
    subscription = assembled["subscription"]
    wallet = assembled["wallet"]
    owner = assembled["owner"]
    entitlements = assembled["entitlements"]

    if wallet is not None:
        wallet_block = {"exists": True, **WalletBlockSerializer(wallet).data}
    else:
        wallet_block = dict(EMPTY_WALLET)

    return {
        "tenant": TenantProfileSerializer(assembled["tenant"]).data,
        "enforcement": {
            # No settings row means the model's defaults, which are both off. Serving
            # `null` would invent a third state the console would have to render.
            "credits_enforced": bool(
                settings_row.credits_enforced if settings_row else False
            ),
            "entitlements_enforced": bool(
                settings_row.entitlements_enforced if settings_row else False
            ),
            "configured": settings_row is not None,
        },
        "owner": (
            {**OwnerSerializer(owner).data, "derived": "first_user"}
            if owner is not None
            else None
        ),
        "user_count": assembled["user_count"],
        "subscription": (
            SubscriptionBlockSerializer(subscription).data
            if subscription is not None
            else None
        ),
        "wallet": wallet_block,
        "entitlements": {
            "rows": entitlements["rows"],
            "reason": entitlements["reason"],
            "purchased_extras": entitlements["purchased_extras"],
        },
        "payments": {
            "rows": PaymentRowSerializer(assembled["payments"], many=True).data,
            # Said out loud because a customer counting their receipts will find one
            # more than this list holds. An onboarding order has `tenant = NULL` by
            # check constraint -- the tenant does not exist until it clears.
            "note": (
                "The onboarding payment is not listed: it is recorded against the "
                "signup record, not the tenant, because the tenant did not exist "
                "when it was made."
            ),
        },
        "ledger": LedgerRowSerializer(assembled["ledger"], many=True).data,
        "usage": assembled["usage"],
        "health": assembled["health"],
    }
