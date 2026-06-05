from rest_framework import serializers
from user.models import CustomUser
from utils.serializers.base_model_core_serializer import BaseModelCoreSerializer
from django.contrib.auth.password_validation import validate_password
from drf_spectacular.utils import extend_schema_field
from tenant.serializers.tenant import TenantRefSerializer
from onboard.serializers.subscription import SubscriptionSerializer


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
    tenant = serializers.SerializerMethodField()
    subscription = serializers.SerializerMethodField()

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
            "tenant",
            "subscription",
        )

        read_only_fields = (
            "id",
            "is_verified",
            "tenant",
            "subscription",
        )

        write_only_fields = (
            "password",
        )

    @extend_schema_field(TenantRefSerializer)
    def get_tenant(self, obj):
        try:
            mapping = obj.tenant_mapping
            tenant = mapping.tenant
            return TenantRefSerializer(tenant).data
        except AttributeError:
            return None

    @extend_schema_field(SubscriptionSerializer)
    def get_subscription(self, obj):
        try:
            mapping = obj.tenant_mapping
            tenant = mapping.tenant
            subscription = tenant.subscriptions.filter(status="active").first()
            if not subscription:
                subscription = tenant.subscriptions.first()
            if not subscription:
                return None
            return SubscriptionSerializer(subscription).data
        except AttributeError:
            return None

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


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(
        required=True,
        style={"input_type": "password"}
    )
    new_password = serializers.CharField(
        required=True,
        min_length=8,
        max_length=32,
        validators=[validate_password],
        style={"input_type": "password"}
    )
    confirm_password = serializers.CharField(
        required=True,
        style={"input_type": "password"}
    )

    def validate_new_password(self, value):
        import re
        if " " in value:
            raise serializers.ValidationError("Password must not contain spaces.")
        if not re.search(r"[A-Z]", value):
            raise serializers.ValidationError("Password must contain at least one uppercase letter.")
        if not re.search(r"[a-z]", value):
            raise serializers.ValidationError("Password must contain at least one lowercase letter.")
        if not re.search(r"\d", value):
            raise serializers.ValidationError("Password must contain at least one digit.")
        if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", value):
            raise serializers.ValidationError("Password must contain at least one special character.")
        return value

    def validate(self, data):
        user = self.context.get("request").user
        if not user.check_password(data["old_password"]):
            raise serializers.ValidationError({"old_password": "Old password is incorrect."})
        if data["new_password"] != data["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "New passwords do not match."})
        if data["old_password"] == data["new_password"]:
            raise serializers.ValidationError({"new_password": "New password cannot be the same as old password."})
        return data