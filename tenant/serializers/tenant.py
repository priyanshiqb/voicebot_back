from rest_framework import serializers
from tenant.models.Tenant import Tenant


class TenantRefSerializer(serializers.ModelSerializer):
    """
    Reference Serializer for Tenant objects.
    Enables highly-performant nested read representation of a Tenant context.
    """
    class Meta:
        model = Tenant
        fields = ("id", "name", "subdomain")
