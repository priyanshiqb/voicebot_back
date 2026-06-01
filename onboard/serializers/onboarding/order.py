from rest_framework import serializers
from django.core.validators import RegexValidator
from .billing import BillingAddressSerializer


class CreateOrderJustInTimeSerializer(serializers.Serializer):
    phone_number = serializers.CharField(
        required=True,
        validators=[
            RegexValidator(
                regex=r"^\d{7,15}$",
                message="Phone number must be between 7 and 15 digits and contain numbers only."
            )
        ]
    )
    billing_address = BillingAddressSerializer(required=True)


class CancelOrderSerializer(serializers.Serializer):
    razorpay_order_id = serializers.CharField(required=True)
