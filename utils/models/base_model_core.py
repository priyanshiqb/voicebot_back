import uuid
from django.db import models
from django.utils import timezone

class BaseQuerySet(models.QuerySet):
    def delete(self):
        raise Exception("Bulk delete is not allowed. Use soft_delete() instead.")

    def soft_delete(self):
        return super().update(is_deleted=True)

    def alive(self):
        return self.filter(is_deleted=False)

    def deleted(self):
        return self.filter(is_deleted=True)

class ExceptDeletedManager(models.Manager):
    def get_queryset(self):
        return BaseQuerySet(
            self.model, 
            using=self._db
        ).filter(is_deleted=False)


class BaseModelCore(models.Model):
    id = models.UUIDField(
        primary_key=True, 
        default=uuid.uuid4, 
        editable=False
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    objects = ExceptDeletedManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        ordering = ['-created_at']

    def soft_delete(self, user=None):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=['is_deleted', 'deleted_at'])

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.save(update_fields=['is_deleted', 'deleted_at'])

    def deactivate(self):
        self.is_active = False
        self.save(update_fields=['is_active'])

    def activate(self):
        self.is_active = True
        self.save(update_fields=['is_active'])
