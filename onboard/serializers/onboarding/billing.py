from rest_framework import serializers
from onboard.models import BillingAddress
import re


class BillingAddressSerializer(serializers.ModelSerializer):
    gstin = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = BillingAddress
        fields = ["line1", "city", "state", "zip_code", "country", "gstin"]

    def validate_gstin(self, value):
        if not value:
            return value
        value = value.strip().upper()
        if len(value) != 15:
            raise serializers.ValidationError("GSTIN must be exactly 15 characters long.")
            
        gst_regex = r"^\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z\d]{1}[Z]{1}[A-Z\d]{1}$"
        if not re.match(gst_regex, value):
            raise serializers.ValidationError("Please enter a valid GSTIN format (e.g. 07AAAAA1111A1Z1).")
        
        try:
            state_code = int(value[:2])
        except ValueError:
            raise serializers.ValidationError("GSTIN state prefix must be numeric.")
            
        valid_state_codes = list(range(1, 39)) + [97, 98, 99]
        if state_code not in valid_state_codes:
            raise serializers.ValidationError("Invalid GSTIN state prefix code.")
            
        return value
