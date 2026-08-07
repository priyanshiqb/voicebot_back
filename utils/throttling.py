"""Clearing the SMTP test-send rate limit.

``/tenant/email-configurations/{id}/test/`` allows a few attempts an hour and
counts attempts rather than successes, so a tenant working through a provider's
setup can exhaust the window on rejected logins before anything is configured —
Gmail in particular, which needs an App Password and not the account password.
This is the support path for that: clear the counter for whoever is stuck, rather
than raise ``SMTP_TEST_THROTTLE_PER_HOUR`` for everybody.

**Deliberately limited to this one scope.** The other throttles in the project
(`contact_import`, `campaign_action`) are not cleared here — an admin action that
lifts every limit at once is a bigger tool than the problem needs, and each of
those has a per-minute window that expires on its own before anyone could
reasonably act on a support request. ``public_reschedule`` could not be cleared
this way regardless: it is an ``AnonRateThrottle`` keyed on client IP, and there
is no mapping from a user back to the addresses they arrived from.
"""

from django.core.cache import cache
from rest_framework.throttling import SimpleRateThrottle

# Must match SMTPTestRateThrottle.scope in tenant.views.tenant_email_configuration.
# Kept as a literal rather than imported from there because that module imports the
# viewset base, which imports this package — `tenant.tests.test_throttle_reset`
# asserts the two agree, so drift fails a test rather than silently clearing keys
# that do not exist.
SMTP_TEST_SCOPE = "smtp_test"


def throttle_cache_key(scope, ident):
    """The cache key DRF stores a throttle history under.

    Built from ``SimpleRateThrottle.cache_format`` rather than an f-string of our
    own, so this follows DRF's format instead of silently missing every key if a
    future version changes it.
    """
    return SimpleRateThrottle.cache_format % {"scope": scope, "ident": ident}


def clear_smtp_test_throttles(user_ids):
    """Delete the SMTP-test throttle history for these users. Returns how many
    were actually cleared.

    The count is of counters that existed — clearing a user who was never
    throttled returns 0, which lets a caller report "nothing to clear" instead of
    implying it unblocked someone. That distinction matters: it means the 429 being
    reported is coming from somewhere else.

    Deletion goes through the cache API, not raw Redis: django-redis stores keys
    under a prefixed, versioned name, so ``redis-cli DEL throttle_smtp_test_<id>``
    removes nothing while appearing to succeed.

    Exceptions are not swallowed. This runs from an explicit admin action, and
    reporting success while the cache is unreachable would send someone back to an
    endpoint that is still blocked.
    """
    cleared = 0
    for user_id in user_ids:
        if cache.delete(throttle_cache_key(SMTP_TEST_SCOPE, user_id)):
            cleared += 1
    return cleared
