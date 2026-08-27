from django.apps import AppConfig


class OpsConfig(AppConfig):
    """The admin console's API — everything platform staff can reach.

    **A separate app that owns no models.** ``Plan``, ``PlanEntitlement``,
    ``CreditRate`` and the rest stay in ``onboard``, ``wallet`` and ``core`` where
    they belong; nothing here declares a model and nothing here has a migration.

    It exists for a boundary that can be audited by inspection rather than by
    convention. The console's central safety rule is *never reuse the tenant-scoped
    viewsets*: those resolve the tenant from ``request.user.tenant_mapping``, and a
    platform operator has none — mount an operator screen on one and it silently
    scopes to the operator's own tenant, returning plausible, wrong data with no
    error. Scattered through ``onboard/views/`` and ``wallet/views/`` that rule holds
    by discipline. In one app behind one router and one permission, "show me
    everything an operator can reach" is a directory listing.

    Owning no models also keeps it out of the migration graph, which matters more than
    it sounds: adding a field to ``user`` already drags later migrations along through
    the swappable ``AUTH_USER_MODEL`` dependency.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "ops"
    verbose_name = "Admin Console"
