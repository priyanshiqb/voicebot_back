from utils.serializers.base_model_core_serializer import (
    BaseModelCoreSerializer
)
from rest_framework import serializers
from utils.serializers.hybrid_related_field import (
    HybridRelatedField,
)
from user.serializers.custom_user import UserRefSerializer
from user.models.CustomUser import CustomUser

class BaseModelSerializer(BaseModelCoreSerializer):

    # created_by_email = serializers.CharField(
    #     source="created_by.email",
    #     read_only=True
    # )

    # updated_by_email = serializers.CharField(
    #     source="updated_by.email",
    #     read_only=True
    # )

    # deleted_by_email = serializers.CharField(
    #     source="deleted_by.email",
    #     read_only=True
    # )
    created_by = HybridRelatedField(
        serializer=UserRefSerializer,
        read_only=True,
    )

    updated_by = HybridRelatedField(
        serializer=UserRefSerializer,
        read_only=True,
    )

    deleted_by = HybridRelatedField(
        serializer=UserRefSerializer,
        read_only=True,
    )

    class Meta(BaseModelCoreSerializer.Meta):
        abstract = True
        read_only_fields = (
            *BaseModelCoreSerializer.Meta.read_only_fields,
            "created_by",
            "updated_by",
            "deleted_by",
        )