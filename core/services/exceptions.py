"""Domain exceptions for the scheduling/booking services.

The view layer maps these to HTTP responses:
    PolicyViolationError / NoEligibleHostError / InvalidTransitionError -> 400
    SlotConflictError                                                   -> 409
"""


class SchedulingError(Exception):
    """Base class for scheduling domain errors."""


class SlotConflictError(SchedulingError):
    """The requested time is no longer available for any eligible host (HTTP 409)."""


class NoEligibleHostError(SchedulingError):
    """No host is configured/available to host the requested event type (HTTP 400)."""


class PolicyViolationError(SchedulingError):
    """A booking policy (min-notice, horizon, lead-time) was violated (HTTP 400)."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


class InvalidTransitionError(SchedulingError):
    """An illegal booking status transition was attempted (HTTP 400)."""
