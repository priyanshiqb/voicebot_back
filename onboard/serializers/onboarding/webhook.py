from rest_framework import serializers


class RazorpayWebhookSerializer(serializers.Serializer):
    id = serializers.CharField(required=True)
    event = serializers.CharField(required=True)
    payload = serializers.JSONField(required=True)
