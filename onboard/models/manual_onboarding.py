from onboard.models.subscription import Subscription


class ManualOnboarding(Subscription):
    """A subscription created by manual (offline-payment) onboarding.

    A proxy, not a table: the subscription *is* the record, and a second model holding
    a copy of it would be one more thing to keep in step. It exists for the Django
    admin -- its own menu entry listing just these subscriptions, and its own
    "Can add manual onboarding" permission, which is what lets a staff account
    onboard customers without being a superuser.

    A manual deal is recognised by ``plan_snapshot["manual_payment_reference"]``,
    which ``onboard.services.manual_onboarding.provision`` always writes.
    """

    class Meta:
        proxy = True
        verbose_name = "Manual onboarding"
        verbose_name_plural = "Manual onboardings"
