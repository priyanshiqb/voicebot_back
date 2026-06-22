from rest_framework import serializers
from onboard.models import Admin3

class Admin3RefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Admin3
        fields = ['id', 'code', 'name']
        read_only_fields = fields

class Admin3DropdownSerializer(serializers.ModelSerializer):
    value = serializers.UUIDField(source='id', read_only=True)
    label = serializers.CharField(source='name', read_only=True)

    class Meta:
        model = Admin3
        fields = ['value', 'label']
