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


class ExternalCalendarUnavailable(SchedulingError):
    """A host's external calendar could not be reached, so their availability
    could not be verified (HTTP 503).

    Deliberately distinct from SlotConflictError: the slot may well be free — we
    simply do not know. Committing a booking on an unverified calendar is how a
    host gets double-booked against an external meeting, which nothing in this
    system can detect afterwards. Browsing degrades open and flags the slot;
    committing refuses and asks the caller to retry.
    """
