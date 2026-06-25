from rest_framework import serializers
from django.core.validators import RegexValidator
from .billing import BillingAddressSerializer


class CreateOrderJustInTimeSerializer(serializers.Serializer):
    phone_number = serializers.CharField(
        required=True,
        validators=[
            RegexValidator(
                regex=r"^[0-9+\s()-]{7,20}$",
                message="Phone number must be between 7 and 20 characters and may contain numbers, spaces, plus signs, and parentheses."
            )
        ]
    )
    billing_address = BillingAddressSerializer(required=True)


class CancelOrderSerializer(serializers.Serializer):
    razorpay_order_id = serializers.CharField(required=True)
