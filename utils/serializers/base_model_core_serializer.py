from rest_framework import serializers


class BaseModelCoreSerializer(serializers.ModelSerializer):

    class Meta:
        abstract = True
        read_only_fields = (
            "id",
            "created_at",
            "updated_at",
            "deleted_at",
            "is_active",
            "is_deleted",
        )