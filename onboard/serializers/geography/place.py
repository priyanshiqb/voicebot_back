from rest_framework import serializers
from onboard.models import Place

class PlaceDropdownSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(read_only=True)
    cities = serializers.CharField(source='name', read_only=True)
    postal_code = serializers.CharField(read_only=True)

    class Meta:
        model = Place
        fields = ['id', 'cities', 'postal_code']

class PlacePostalCodeDropdownSerializer(serializers.ModelSerializer):
    value = serializers.UUIDField(source='id', read_only=True)
    label = serializers.SerializerMethodField()

    class Meta:
        model = Place
        fields = ['value', 'label']

    def get_label(self, obj) -> str:
        return f"{obj.name} | {obj.postal_code}"

from utils.serializers.hybrid_related_field import HybridRelatedField
from .country import CountryRefSerializer
from .admin1 import Admin1RefSerializer
from .admin2 import Admin2RefSerializer
from .admin3 import Admin3RefSerializer

class PlaceSerializer(serializers.ModelSerializer):
    country = HybridRelatedField(serializer=CountryRefSerializer, read_only=True)
    admin1 = HybridRelatedField(serializer=Admin1RefSerializer, read_only=True)
    admin2 = HybridRelatedField(serializer=Admin2RefSerializer, read_only=True)
    admin3 = HybridRelatedField(serializer=Admin3RefSerializer, read_only=True)

    class Meta:
        model = Place
        fields = ['id', 'country', 'admin1', 'admin2', 'admin3', 'postal_code', 'name']
        read_only_fields = fields
