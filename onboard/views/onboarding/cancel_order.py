from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from onboard.models import TempUser, BillingAddress, Transaction
from onboard.authentication import TempUserAuthentication
from onboard.views.onboarding.base import IsTempUserAuthenticated
from onboard.serializers.onboarding import CancelOrderSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 4 Helper: Cancel Order / Discard Progress",
    description="Discards current registration progress, cleans up database records for the temporary user, and cancels the Razorpay order state.",
    responses={200: GeneralResponseSerializer}
)
class CancelOrderView(APIView):
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

        return Response({
            "status": True,
            "message": "Order cancelled and deactivated successfully",
            "data": None,
            "errors": None
        }, status=status.HTTP_200_OK)
