from utils.admin.base_admin_core import (
    BaseAdminCore
)


class BaseAdmin(BaseAdminCore):

    readonly_fields = (
        *BaseAdminCore.readonly_fields,

        "created_by",
        "updated_by",
        "deleted_by",
    )

    autocomplete_fields = (
        "created_by",
        "updated_by",
        "deleted_by",
    )

    def delete_model(self, request, obj):
        super().delete_model(
            request,
            obj
        )

    def delete_queryset(
        self,
        request,
        queryset
    ):
        queryset.delete()

    def soft_delete_selected(
        self,
        request,
        queryset
    ):

        for obj in queryset:

            if hasattr(obj, "soft_delete"):
                obj.soft_delete(
                    user=request.user
                )   