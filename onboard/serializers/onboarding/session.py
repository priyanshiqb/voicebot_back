from rest_framework import serializers


class ResumeSessionSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    temp_registration_id = serializers.UUIDField(required=False, allow_null=True)
    password = serializers.CharField(
        required=True,
        style={"input_type": "password"},
        help_text="The password chosen during step 1 of onboarding."
    )

    def validate(self, data):
        email = data.get("email")
        temp_registration_id = data.get("temp_registration_id")
        if not email and not temp_registration_id:
            raise serializers.ValidationError(
                "Either email or temp_registration_id must be provided."
            )
        return data
