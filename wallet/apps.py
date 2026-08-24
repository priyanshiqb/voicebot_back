from django.apps import AppConfig


class WalletConfig(AppConfig):
    """
    The centralised credit wallet.

    A separate app rather than a package inside ``onboard`` because of who talks to
    it. ``core`` is what spends credits — every call that lands in ``AgentCallLog``
    is a deduction — and ``onboard`` is what funds them. Housing the wallet in
    ``onboard`` would make the product's main app import from the *signup* app on
    the call path, which is the wrong dependency to normalise.

    The wallet depends on ``tenant`` (a wallet belongs to a tenant) and names
    ``onboard.Plan`` by string for the rate-card stamp. It never imports either at
    module load, and it never reads a Subscription to make a decision.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "wallet"
    verbose_name = "Credit Wallet"
