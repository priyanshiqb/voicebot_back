from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from user.models import CustomUser


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):

    model = CustomUser

    ordering = ("-created_at",)

    list_display = (
        "email",
        "first_name",
        "last_name",
        "is_active",
        "is_verified",
        "is_staff",
        "created_at",
    )

    list_filter = (
        "is_active",
        "is_verified",
        "is_staff",
        "is_superuser",
        "is_deleted",
    )

    search_fields = (
        "id",
        "email",
        "first_name",
        "last_name",
        "phone_number",
    )

    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "deleted_at",
    )

    fieldsets = (

        ("Authentication", {
            "fields": (
                "email",
                "password",
            )
        }),

        ("Personal Info", {
            "fields": (
                "first_name",
                "last_name",
                "username",
                "phone_number",
            )
        }),

        ("Permissions", {
            "fields": (
                "is_active",
                "is_verified",

                "is_staff",
                "is_superuser",

                "groups",
                "user_permissions",
            )
        }),

        ("Audit", {
            "fields": (
                "created_by",
                "updated_by",
                "deleted_by",

                "created_at",
                "updated_at",
                "deleted_at",

                "is_deleted",
            )
        }),
    )

    add_fieldsets = (

        (None, {
            "classes": ("wide",),

            "fields": (
                "email",

                "password1",
                "password2",

                "is_active",
                "is_verified",

                "is_staff",
                "is_superuser",
            ),
        }),
    )