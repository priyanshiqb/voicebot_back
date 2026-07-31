from import_export import fields, resources


class TenantModelResource(resources.ModelResource):
    """
    Injects tenant and audit context into rows during import.

    `tenant`, `user` and `allow_cross_tenant` are set on the resource instance by the
    caller (see `ImportExportMixin._run_import`) before `import_data()` runs.
    """

    # Declared so an unset scope is an explicit, inspectable state rather than a missing
    # attribute. `tenant = None` with `allow_cross_tenant = False` means "no scope was
    # established" — which get_queryset below treats as deny, not as unrestricted.
    tenant = None
    user = None
    allow_cross_tenant = False

    # `import_id_fields` defaults to ("id",) and the resource resolves it through
    # self.fields, so `id` must be declared or every import raises KeyError.
    #
    # readonly=True means the column identifies an existing row but is never written
    # onto the instance. Combined with the tenant-scoped get_queryset below, a CSV
    # carrying another tenant's UUID neither updates their record nor collides with
    # its primary key — it simply becomes a new row in the caller's own tenant.
    id = fields.Field(attribute="id", column_name="id", readonly=True)

    def get_queryset(self):
        """
        Scope instance lookup to the importing user's tenant.

        import-export resolves create-vs-update by looking the row's `id` up in this
        queryset. Unscoped, a crafted CSV carrying another tenant's UUID would update
        their record. Every model using this base is tenant-partitioned, so the filter
        is the boundary.

        FAILS CLOSED. This previously skipped the filter whenever `tenant` was unset,
        which read an absent scope as "no scope needed" and made the boundary vanish for
        any caller without a tenant mapping — VB_AGENT tokens could overwrite any
        tenant's rows by putting their ids in the CSV. An unset tenant now matches
        nothing, so every such row is treated as a create and fails on the non-null
        tenant column rather than silently landing on someone else's record.

        Global access is opt-in via `allow_cross_tenant`, which the mixin sets only for
        superusers — the same carve-out `TenantBaseViewSet` makes everywhere else.
        """
        queryset = super().get_queryset()

        if not any(f.name == "tenant" for f in queryset.model._meta.get_fields()):
            return queryset

        if self.tenant is not None:
            return queryset.filter(tenant=self.tenant)

        if self.allow_cross_tenant:
            return queryset

        return queryset.none()

    def before_save_instance(self, instance, row, **kwargs):
        # Signature must match import-export 4.x, which calls this as
        # before_save_instance(instance, row, **kwargs). The old
        # (instance, using_transactions, dry_run) form raises TypeError and takes the
        # whole import endpoint down with it.
        tenant = getattr(self, "tenant", None)
        if tenant and not getattr(instance, "tenant_id", None):
            instance.tenant = tenant

        user = getattr(self, "user", None)
        if user:
            field_names = {f.name for f in instance._meta.get_fields()}

            # Every model here defaults its pk to uuid4, so `instance.pk` is already
            # populated on a brand-new object and cannot be used to detect creation.
            # `_state.adding` is the only reliable signal.
            if "created_by" in field_names and instance._state.adding:
                instance.created_by = user

            # Guarded because Tier 1 (Core) models have no audit columns — assigning
            # them there is a silent no-op rather than an error.
            if "updated_by" in field_names:
                instance.updated_by = user

        super().before_save_instance(instance, row, **kwargs)
