from rest_framework import serializers
from user.models import CustomUser
from utils.serializers.base_model_core_serializer import BaseModelCoreSerializer
from django.contrib.auth.password_validation import validate_password


class UserRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
        )
        read_only_fields = fields


class UserSerializer(BaseModelCoreSerializer):
    password = serializers.CharField(
        write_only=True,
        required=False,
        validators=[validate_password],
        style={"input_type": "password"},
    )

    class Meta(BaseModelCoreSerializer.Meta):
        model = CustomUser

        fields = (
            "id",
            "email",
            "password",
            "first_name",
            "last_name",
            "phone_number",
            "is_active",
            "is_verified",
        )

        read_only_fields = (
            "id",
            "is_verified",
        )

        write_only_fields = (
            "password",
        )

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        return CustomUser.objects.create_user(
            password=password,
            **validated_data
        )
    
    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        if password:
            instance.set_password(password)
        instance.save()
        return instance
    

class UserListSerializer(BaseModelCoreSerializer):
    class Meta(BaseModelCoreSerializer.Meta):
        model = CustomUser

        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "phone_number",
            "is_active",
            "is_verified",
            "created_at",
        )


class UserDropdownSerializer(serializers.ModelSerializer):
    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id")

    class Meta:
        model = CustomUser

        fields = (
            "label",
            "value",
        )

        read_only_fields = fields

    def get_label(self, obj):
        if obj.first_name and obj.last_name:
            return f"{obj.first_name} {obj.last_name}"
        elif obj.first_name:
            return obj.first_name
        elif obj.username:
            return obj.username
        else:
            return obj.email