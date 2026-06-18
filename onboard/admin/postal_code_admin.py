from django.contrib import admin
from onboard.models.postal_code import PostalCode


@admin.register(PostalCode)
class PostalCodeAdmin(admin.ModelAdmin):
    """
    PostalCode ModelAdmin interface configuration.
    Registers PostalCode model with searches, filters, list displays, and fieldsets.
    """
    model = PostalCode

    list_display = (
        "country_code",
        "postal_code",
        "place_name",
        "admin_name1",
        "admin_code1",
        "latitude",
        "longitude",
        "is_active",
    )

    list_filter = (
        "country_code",
        "is_active",
    )

    search_fields = (
        "id",
        "postal_code",
        "place_name",
        "admin_name1",
        "country_code",
    )

    readonly_fields = (
        "id",
        "coordinates",
    )

    ordering = ("country_code", "postal_code", "place_name")

    fieldsets = (
        ("General Details", {
            "fields": (
                "id",
                "country_code",
                "postal_code",
                "place_name",
                "is_active",
            )
        }),
        ("Administrative Subdivisions", {
            "fields": (
                "admin_name1",
                "admin_code1",
                "admin_name2",
                "admin_code2",
                "admin_name3",
                "admin_code3",
            )
        }),
        ("Location Metrics", {
            "fields": (
                "latitude",
                "longitude",
                "accuracy",
                "coordinates",
            )
        }),
    )

    actions = ("activate_selected", "deactivate_selected")

    @admin.action(description="Activate selected items")
    def activate_selected(self, request, queryset):
        queryset.update(is_active=True)

    @admin.action(description="Deactivate selected items")
    def deactivate_selected(self, request, queryset):
        queryset.update(is_active=False)

