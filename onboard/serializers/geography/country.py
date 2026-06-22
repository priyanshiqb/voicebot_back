from rest_framework import serializers
from onboard.models import Country

class CountryRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Country
        fields = ['id', 'code', 'name']
        read_only_fields = fields

class CountryDropdownSerializer(serializers.ModelSerializer):
    value = serializers.UUIDField(source='id', read_only=True)
    label = serializers.CharField(source='name', read_only=True)

    class Meta:
        model = Country
        fields = ['value', 'label']
