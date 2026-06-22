from rest_framework import serializers
from onboard.models import Admin1

class Admin1RefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Admin1
        fields = ['id', 'code', 'name']
        read_only_fields = fields

class Admin1DropdownSerializer(serializers.ModelSerializer):
    value = serializers.UUIDField(source='id', read_only=True)
    label = serializers.CharField(source='name', read_only=True)

    class Meta:
        model = Admin1
        fields = ['value', 'label']
