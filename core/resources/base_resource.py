from import_export import resources

class TenantModelResource(resources.ModelResource):
    def before_save_instance(self, instance, using_transactions, dry_run):
        if getattr(self, 'tenant', None) and not getattr(instance, 'tenant_id', None):
            instance.tenant = self.tenant
        if getattr(self, 'user', None):
            if not instance.pk:
                instance.created_by = self.user
            instance.updated_by = self.user
        super().before_save_instance(instance, using_transactions, dry_run)
