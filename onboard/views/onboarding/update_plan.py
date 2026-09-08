from rest_framework.views import APIView
from rest_framework import status

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import Plan
from onboard.models.plan import MAX_SELECTABLE_MINUTES, coerce_minutes
from onboard.authentication import TempUserAuthentication
from onboard.views.onboarding.base import (
    IsTempUserAuthenticated,
    billing_breakdown as build_breakdown,
)
from onboard.serializers.onboarding import UpdatePlanSerializer, UpdatePlanResponseSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 3: Update Selected Plan",
    description="Updates the user's selected subscription plan and returns a dynamic breakdown of the new pricing.",
    responses={
        200: UpdatePlanResponseSerializer,
        400: GeneralResponseSerializer,
        401: GeneralResponseSerializer,
        403: GeneralResponseSerializer
    }
)
class UpdatePlanView(ResponseMixin, APIView):
    authentication_classes = [TempUserAuthentication]
    permission_classes = [IsTempUserAuthenticated]
    serializer_class = UpdatePlanSerializer

    def patch(self, request):
        plan_id = request.data.get("plan_id")
        temp_user = request.user

        try:
            plan = Plan.objects.get(id=plan_id, is_active=True)
        except (Plan.DoesNotExist, ValueError):
            return self.error(
                message="Plan not found or inactive",
                errors=["PLAN_NOT_FOUND"],
                status_code=status.HTTP_404_NOT_FOUND
            )

        # **Re-validated against the new plan, not carried over.** A registrant on
        # 1,200 minutes who switches to a plan selling up to 500 has to be stopped
        # here; letting it through means the quantity and the plan disagree until
        # checkout, where the failure is much less explicable.
        raw = request.data.get("minutes", temp_user.selected_minutes)
        minutes = coerce_minutes(raw)
        if plan.is_minutes_based:
            if minutes is None:
                return self.error(
                    message=f"{plan.name} is sold by the minute; tell us how many.",
                    errors=["MINUTES_REQUIRED"],
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if minutes > MAX_SELECTABLE_MINUTES:
                return self.error(
                    message=(
                        f"Minutes must be between 1 and "
                        f"{MAX_SELECTABLE_MINUTES:,}."
                    ),
                    errors=["MINUTES_OUT_OF_BAND"],
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if not plan.contains_minutes(minutes):
                return self.error(
                    message=f"{plan.name} does not sell {minutes:,} minutes.",
                    errors=["MINUTES_OUT_OF_BAND"],
                    data={
                        "min_minutes": plan.min_minutes,
                        "max_minutes": plan.max_minutes,
                    },
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
        else:
            minutes = None

        temp_user.selected_plan = plan
        temp_user.selected_minutes = minutes
        temp_user.save()

        billing_breakdown = build_breakdown(plan, temp_user.selected_minutes)

        return self.success(
            data={
                "plan": {
                    "id": str(plan.id),
                    "name": plan.name,
                    "code": plan.code
                },
                "billing_breakdown": billing_breakdown
            },
            message="Plan updated successfully",
            status_code=status.HTTP_200_OK
        )

