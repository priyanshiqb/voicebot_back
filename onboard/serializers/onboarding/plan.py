from rest_framework import serializers


class UpdatePlanSerializer(serializers.Serializer):
    plan_id = serializers.UUIDField(required=True)
