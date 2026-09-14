from drf_spectacular.utils import extend_schema
from rest_framework import status

from onboard.authentication import TempUserAuthentication
from onboard.serializers.onboarding import (
    BillingBreakdownEnvelopeSerializer,
    GeneralResponseSerializer,
)
from onboard.views.onboarding.base import (
    IsTempUserAuthenticated,
    billing_breakdown as build_breakdown,
)
from utils.views.base_api_view import BaseAPIView


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Billing breakdown for the registrant's plan and minutes",
    description=(
        "What the registrant will be charged for the plan and quantity they have "
        "chosen, and what they get for it. The same numbers `verify-otp/` returned, "
        "fetched again on demand -- for a page that has been refreshed, or a pricing "
        "summary that re-renders.\n\n"
        "**Read-only, with no side effects.** Unlike `resume-session-token/` it does "
        "not reissue the token or work out the next step, so it is safe to call as "
        "often as a component re-renders.\n\n"
        "**`sellable` predicts checkout.** It runs the same check "
        "`create-order-just-in-time/` does. `false` means the plan's band has moved "
        "since the minutes were chosen, and checkout would refuse with "
        "`MINUTES_NOT_SELLABLE`; `reason` says why. The breakdown is still returned so "
        "the page can show what changed."
    ),
    request=None,
    responses={
        200: BillingBreakdownEnvelopeSerializer,
        400: GeneralResponseSerializer,
        401: GeneralResponseSerializer,
        403: GeneralResponseSerializer,
    },
)
class BillingBreakdownView(BaseAPIView):
    # An onboarding endpoint: only a pending registration, holding its temp token, can
    # reach it. Both of the base's defaults are replaced -- the temp-token authenticator
    # alone, and IsTempUserAuthenticated in place of IsAuthenticated, since a signed-in
    # tenant user has a subscription rather than a registration. VBAgentActionPermission
    # is dropped: with TempUserAuthentication as the only authenticator an agent token
    # cannot authenticate here, so there is no machine caller for it to narrow.
    authentication_classes = (TempUserAuthentication,)
    permission_classes = (IsTempUserAuthenticated,)

    def get(self, request):
        from onboard.services import pricing

        temp_user = request.user
        plan = temp_user.selected_plan

        if plan is None:
            return self.error(
                message="No plan selected",
                errors=["PLAN_SELECTION_REQUIRED"],
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        minutes = temp_user.selected_minutes

        # **The same check create-order runs, so this cannot disagree with checkout.**
        # A price the page shows and checkout then refuses is worse than no price at
        # all -- the customer has already been told a number. Reached when a plan's
        # band is edited between the registrant choosing minutes and paying.
        sellable, reason = True, None
        if plan.is_minutes_based:
            try:
                pricing.quote_plan(plan, minutes)
            except pricing.PricingError as exc:
                sellable, reason = False, str(exc)

        return self.success(
            data={
                "plan": {
                    "id": str(plan.id),
                    "name": plan.name,
                    "code": plan.code,
                    "billing_cycle": plan.billing_cycle,
                    "currency": plan.currency,
                    # The band, so a refreshed page can re-render a minutes selector
                    # without a second request to the plans API.
                    "min_minutes": plan.min_minutes,
                    "max_minutes": plan.max_minutes,
                    "is_fixed_quantity": plan.is_fixed_quantity,
                    "is_minutes_based": plan.is_minutes_based,
                },
                "selected_minutes": minutes,
                "billing_breakdown": build_breakdown(plan, minutes),
                "sellable": sellable,
                "reason": reason,
            },
            message="Billing breakdown retrieved",
            status_code=status.HTTP_200_OK,
        )
