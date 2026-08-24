class WalletError(Exception):
    """Base for every wallet-service failure."""


class RateNotConfigured(WalletError):
    """No price could be resolved for an operation.

    Raised loudly rather than defaulting to zero. A platform that silently charges
    nothing for an unpriced operation gives its product away, and the first symptom is
    a revenue gap nobody can date. `report_unrated_calls` catches anything that still
    slips past this.
    """


class InsufficientCredits(WalletError):
    """The tenant cannot afford this, and enforcement is on.

    Maps to HTTP 402 at the view layer. Raised only when
    ``TenantSettings.credits_enforced`` is true — otherwise a shortfall is logged and
    the work proceeds.
    """

    def __init__(self, required, available, message=None):
        self.required = required
        self.available = available
        super().__init__(
            message or f"Requires {required} credits, wallet holds {available}."
        )


class WalletFrozen(WalletError):
    """Spending is blocked on this wallet.

    Distinct from ``InsufficientCredits`` because the remedy is different: a frozen
    wallet needs a renewal or a support action, not a top-up.
    """
