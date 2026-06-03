from rest_framework.views import APIView
from rest_framework import status

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser, BillingAddress, Transaction
from onboard.authentication import TempUserAuthentication
from onboard.views.onboarding.base import IsTempUserAuthenticated
from onboard.serializers.onboarding import CancelOrderSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 4 Helper: Cancel Order / Discard Progress",
    description="Discards current registration progress, cleans up database records for the temporary user, and cancels the Razorpay order state.",
    responses={
        200: GeneralResponseSerializer,
        400: GeneralResponseSerializer,
        401: GeneralResponseSerializer,
        403: GeneralResponseSerializer
    }
)
class CancelOrderView(ResponseMixin, APIView):
    authentication_classes = [TempUserAuthentication]
    permission_classes = [IsTempUserAuthenticated]
    serializer_class = CancelOrderSerializer

    def post(self, request):
        temp_user = request.user
        order_id = request.data.get("razorpay_order_id")

        try:
            transaction = Transaction.objects.get(razorpay_order_id=order_id, temp_user=temp_user)
            transaction.status = Transaction.TransactionStatusChoices.CANCELED
            transaction.is_active = False
            transaction.save()
            transaction.soft_delete()
        except Transaction.DoesNotExist:
            pass

        return self.success(
            message="Order cancelled and deactivated successfully",
            status_code=status.HTTP_200_OK
        )

