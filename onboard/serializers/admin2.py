from rest_framework import serializers
from onboard.models import Admin2

class Admin2RefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Admin2
        fields = ['id', 'code', 'name']
        read_only_fields = fields

class Admin2DropdownSerializer(serializers.ModelSerializer):
    value = serializers.UUIDField(source='id', read_only=True)
    label = serializers.CharField(source='name', read_only=True)

    class Meta:
        model = Admin2
        fields = ['value', 'label']
