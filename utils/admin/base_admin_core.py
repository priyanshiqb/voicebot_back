from django.contrib import admin


class BaseAdminCore(admin.ModelAdmin):

    def get_queryset(self, request):
        if hasattr(self.model, "all_objects"):
            return self.model.all_objects.get_queryset()
        return super().get_queryset(request)

    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "deleted_at",
    )

    ordering = (
        "-created_at",
    )

    list_filter = (
        "is_active",
        "is_deleted",
        "created_at",
    )

    search_fields = (
        "id",
    )

    save_on_top = True


    actions = (
        "soft_delete_selected",
        "restore_selected",
        "activate_selected",
        "deactivate_selected",
    )

    def delete_model(self, request, obj):

        if hasattr(obj, "soft_delete"):
            obj.soft_delete()
        else:
            super().delete_model(
                request,
                obj
            )

    def delete_queryset(
        self,
        request,
        queryset
    ):

        for obj in queryset:

            if hasattr(obj, "soft_delete"):
                obj.soft_delete()
            else:
                obj.delete()

    @admin.action(
        description="Soft delete selected items"
    )
    def soft_delete_selected(
        self,
        request,
        queryset
    ):

        for obj in queryset:

            if hasattr(obj, "soft_delete"):
                obj.soft_delete()

    @admin.action(
        description="Restore selected items"
    )
    def restore_selected(
        self,
        request,
        queryset
    ):

        for obj in queryset:

            if hasattr(obj, "restore"):
                obj.restore()

    @admin.action(
        description="Activate selected items"
    )
    def activate_selected(
        self,
        request,
        queryset
    ):

        queryset.update(is_active=True)

    @admin.action(
        description="Deactivate selected items"
    )
    def deactivate_selected(
        self,
        request,
        queryset
    ):

        queryset.update(is_active=False)