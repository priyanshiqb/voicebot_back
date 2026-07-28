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

    def validate_phone_number(self, value):
        from django.contrib.auth import get_user_model
        from onboard.models import TempUser
        
        User = get_user_model()
        phone = value.strip()
        
        # We must exclude the CURRENT TempUser from the pending registrations check
        # otherwise they couldn't submit their own phone number again
        if User.objects.filter(phone_number=phone).exists():
            raise serializers.ValidationError("A user with this phone number already exists.")
            
        request = self.context.get("request")
        current_temp_user_id = request.user.id if request and hasattr(request, "user") else None
        
        pending_qs = TempUser.objects.filter(phone_number=phone)
        if current_temp_user_id:
            pending_qs = pending_qs.exclude(id=current_temp_user_id)
            
        if pending_qs.exists():
            raise serializers.ValidationError("A registration is already pending for this phone number.")
            
        return phone


class CancelOrderSerializer(serializers.Serializer):
    razorpay_order_id = serializers.CharField(required=True)
