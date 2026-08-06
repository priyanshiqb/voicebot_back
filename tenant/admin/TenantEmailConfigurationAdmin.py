from django import forms
from django.contrib import admin

from tenant.common import TenantBaseAdmin
from tenant.models.TenantEmailConfiguration import TenantEmailConfiguration


class TenantEmailConfigurationForm(forms.ModelForm):
    """Admin form that treats the stored password as write-only.

    The column holds ciphertext, so binding it to a normal field would render the
    encrypted blob into the page and then save that blob back through
    ``set_password`` — double-encrypting a working credential on any unrelated
    edit. The field is always rendered empty and only written when someone types
    something.
    """

    smtp_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        label="SMTP password",
        help_text="Leave blank to keep the current password.",
    )

    class Meta:
        model = TenantEmailConfiguration
        exclude = ("password",)

    def save(self, commit=True):
        instance = super().save(commit=False)
        raw = self.cleaned_data.get("smtp_password")
        if raw:
            instance.set_password(raw)
        if commit:
            instance.save()
        return instance


@admin.register(TenantEmailConfiguration)
class TenantEmailConfigurationAdmin(TenantBaseAdmin):
    """
    Read-mostly view of a tenant's outbound mail credentials.

    Deliberately has **no send action**. Verification belongs to the API's
    per-configuration test endpoint, which is throttled and pins the recipient to
    the caller — an admin action that mails an arbitrary address would route
    around both.
    """

    form = TenantEmailConfigurationForm

    list_display = (
        "tenant",
        "name",
        "provider",
        "host",
        "port",
        "from_email",
        "status",
        "is_default",
        "is_active",
    )
    list_filter = ("provider", "status", "is_default", "is_active", "is_deleted")
    search_fields = ("name", "host", "from_email", "tenant__name", "id")

    # Lifecycle state is written by the test send and the send path. Editing it by
    # hand would let someone mark an untested configuration VERIFIED, which is the
    # one status that decides whether real customer mail flows through it.
    readonly_fields = tuple(
        set(
            TenantBaseAdmin.readonly_fields
            + (
                "is_default",
                "status",
                "verified_at",
                "last_tested_at",
                "last_used_at",
                "last_error",
            )
        )
    )

    fieldsets = (
        (None, {"fields": ("tenant", "name", "provider", "is_default", "is_active")}),
        (
            "Connection",
            {"fields": ("host", "port", "username", "smtp_password", "use_tls", "use_ssl")},
        ),
        ("Identity", {"fields": ("from_email", "from_name", "reply_to")}),
        (
            "Status",
            {
                "fields": (
                    "status",
                    "verified_at",
                    "last_tested_at",
                    "last_used_at",
                    "last_error",
                )
            },
        ),
    )

    def save_model(self, request, obj, form, change):
        """Re-check the default after any admin edit.

        `is_active` is editable here — support staff need to be able to pull a
        misbehaving configuration out of service. But toggling it off through the
        form bypasses the disconnect endpoint, and a disconnected default sends
        nothing while still satisfying the unique constraint. Running the same
        check the API runs keeps the two paths honest.
        """
        super().save_model(request, obj, form, change)
        TenantEmailConfiguration.ensure_default(obj.tenant_id)

    def delete_model(self, request, obj):
        """Promote a replacement when the default is removed here too.

        The API route maintains this invariant; without the same handling in
        admin, deleting a default from this page leaves the tenant with
        configurations and no default — and mail silently reverts to the platform
        sender.
        """
        tenant_id = obj.tenant_id
        super().delete_model(request, obj)
        TenantEmailConfiguration.ensure_default(tenant_id)

    def delete_queryset(self, request, queryset):
        tenant_ids = list(queryset.values_list("tenant_id", flat=True).distinct())
        super().delete_queryset(request, queryset)
        for tenant_id in tenant_ids:
            TenantEmailConfiguration.ensure_default(tenant_id)
