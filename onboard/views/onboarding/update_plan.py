from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from onboard.models import Plan
from onboard.authentication import TempUserAuthentication
from onboard.views.onboarding.base import IsTempUserAuthenticated
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
class UpdatePlanView(APIView):
    authentication_classes = [TempUserAuthentication]
    permission_classes = [IsTempUserAuthenticated]
    serializer_class = UpdatePlanSerializer

    def patch(self, request):
        plan_id = request.data.get("plan_id")
        temp_user = request.user

        try:
            plan = Plan.objects.get(id=plan_id, is_active=True)
        except (Plan.DoesNotExist, ValueError):
            return Response({
                "status": False,
                "message": "Plan not found or inactive",
                "data": None,
                "errors": ["PLAN_NOT_FOUND"]
            }, status=status.HTTP_404_NOT_FOUND)

        # Update the selected plan in the TempUser record
        temp_user.selected_plan = plan
        temp_user.save()

        # Compute updated price breakdown dynamically
        billing_breakdown = {
            "base_price": str(plan.base_price),
            "discount_amount": str(plan.discount_amount),
            "net_base_price": str(plan.net_base_price),
            "tax_percentage": str(plan.tax_percentage),
            "tax_amount": str(plan.tax_amount),
            "total_price": str(plan.total_price),
            "currency": plan.currency
        }

        return Response({
            "status": True,
            "message": "Plan updated successfully",
            "data": {
                "plan": {
                    "id": str(plan.id),
                    "name": plan.name,
                    "code": plan.code
                },
                "billing_breakdown": billing_breakdown
            },
            "errors": None
        }, status=status.HTTP_200_OK)
