from django.utils import timezone

from utils.models.base_model_core import BaseModelCore
from django.conf import settings
from django.db import models

class BaseModel(BaseModelCore):

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='%(app_label)s_%(class)s_created_by'
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='%(app_label)s_%(class)s_updated_by'
    )

    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='%(app_label)s_%(class)s_deleted_by'
    )

    class Meta:
        abstract = True

    def soft_delete(self, user=None):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        if user:
            self.deleted_by = user
            self.save(update_fields=['is_deleted', 'deleted_by', 'deleted_at'])
        else:
            self.save(update_fields=['is_deleted', 'deleted_at'])