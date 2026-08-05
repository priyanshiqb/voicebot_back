"""
Post-commit side effects that must never fail the request that scheduled them.

``transaction.on_commit`` callbacks run AFTER the transaction commits, but still
inside the request/response cycle. An exception raised there — a broker that is
unreachable, a provider blip — propagates to the caller as a 500 even though the
booking is already durably written.

That failure mode is the bad one: the customer is booked and believes they are
not. They retry, and the second attempt 409s against their own first booking.

Wrapping the callback converts that into a logged error plus a correct 2xx. The
work is lost rather than the response, which is the right trade for a
notification or a calendar mirror — both are recoverable, a confused customer is
not. (The durable version of this is a transactional outbox; this is the small
version that removes the 500.)
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def safe(fn, *args, label=None, **kwargs):
    """Return a zero-arg callable for ``transaction.on_commit`` that cannot raise.

    Failures are logged with a traceback. Nothing here retries: Celery tasks own
    their own retry policy, and the failure this guards against is *reaching* the
    broker at all.

    Under ``CELERY_TASK_ALWAYS_EAGER`` the exception is re-raised. Eager mode
    means the task body runs inline — i.e. a test — and swallowing there would
    turn a genuinely broken task into a silently passing test. Production
    swallows; tests surface.
    """
    name = label or getattr(fn, "__name__", repr(fn))

    def _run():
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.exception(
                "deferred side effect %r failed after commit; the database "
                "change it accompanies is already durable",
                name,
            )
            if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
                raise

    return _run
