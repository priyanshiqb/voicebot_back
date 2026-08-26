from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from user.models import CustomUser
from utils.throttling import clear_smtp_test_throttles


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):

    model = CustomUser

    ordering = ("-created_at",)

    actions = ("clear_smtp_test_rate_limit",)

    @admin.action(description="Clear SMTP test rate limit for selected users")
    def clear_smtp_test_rate_limit(self, request, queryset):
        """Unblock users sitting on the SMTP test-send window.

        That endpoint allows a few attempts an hour and counts failures the same
        as successes, so configuring a provider that needs an App Password can
        exhaust it before anything works.
        """
        user_ids = list(queryset.values_list("pk", flat=True))
        cleared = clear_smtp_test_throttles(user_ids)

        if cleared:
            self.message_user(
                request,
                f"Cleared the SMTP test rate limit for {cleared} of "
                f"{len(user_ids)} selected user(s).",
                messages.SUCCESS,
            )
        else:
            # Distinguished from success on purpose: "nothing was throttled" and
            # "unblocked you" look identical otherwise, and the first means the
            # 429 someone reported is coming from somewhere else.
            self.message_user(
                request,
                f"None of the {len(user_ids)} selected user(s) were rate limited "
                f"on the SMTP test endpoint.",
                messages.INFO,
            )

    def get_actions(self, request):
        """Superusers only.

        The throttles this clears are protections, not preferences — the SMTP one
        bounds outbound connections to a host the caller names. Anyone who can
        reset their own limit at will is not throttled.
        """
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop("clear_smtp_test_rate_limit", None)
        return actions

    list_display = (
        "email",
        "first_name",
        "last_name",
        "user_type",
        "platform_role",
        "is_active",
        "is_verified",
        "is_staff",
        "created_at",
    )

    list_filter = (
        "user_type",
        "platform_role",
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
                "session_timeout_hours",
            )
        }),

        ("Permissions", {
            "fields": (
                "user_type",
                "platform_role",
                "console_permissions",

                "is_active",
                "is_verified",

                "is_staff",
                "is_superuser",

                "groups",
                "user_permissions",
            ),
            "description": (
                "<b>platform_role is the admin console gate</b>, and it is separate "
                "from is_staff and is_superuser on purpose. Those two grant Django "
                "admin — engineering's escape hatch. A console operator should "
                "have a platform_role and neither of the others.<br><br>"
                "<b>MANAGER</b> runs the whole panel and needs no grants. "
                "<b>SUB_MANAGER</b> starts able to read and nothing else; each "
                "further action is listed in console_permissions, using the codes "
                "from the operator audit log (credits.grant, payment.replay, and so "
                "on).<br><br>"
                "Setting a platform_role requires user_type = ADMIN; the database "
                "refuses the pair otherwise, so a half-configured operator cannot "
                "exist."
            ),
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
                
                "user_type",

                "is_active",
                "is_verified",

                "is_staff",
                "is_superuser",
            ),
        }),
    )