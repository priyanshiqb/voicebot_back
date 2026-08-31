"""Rejection of throwaway inboxes at signup.

A disposable address costs nothing to create and expires on its own, so it defeats
the two things the onboarding email is actually for: proving the person can be
reached again, and giving support a thread to pull when something goes wrong with
their account. A ``payment.authorized`` for a paid plan has already arrived against
a ``@yopmail.com`` address, which is what prompted this.

**The list is data, not logic.** ``disposable-email-domains`` ships ~8k domains and
is refreshed upstream; bump the pin to pick up new ones rather than editing anything
here. Two settings sit on top of it for the cases a pinned list cannot cover:

- ``DISPOSABLE_EMAIL_EXTRA_BLOCKED`` — domains to reject that the package has not
  caught up with yet. New throwaway providers appear constantly and a dependency
  bump needs a deploy.
- ``DISPOSABLE_EMAIL_ALLOWLIST`` — an escape hatch, and the more important of the
  two. The upstream list is community-maintained and occasionally sweeps up a domain
  a real customer uses; without an override, that customer simply cannot sign up and
  nobody finds out until they give up and leave.

**Subdomains are walked upward.** ``user@mail.yopmail.com`` is as disposable as
``user@yopmail.com``, and treating them differently would make the check trivially
avoidable. The walk stops at two labels so it never tests a bare TLD. The allowlist
is consulted at each level *before* the blocklist, so allowing a specific subdomain
of a blocked parent works.

This is deliberately a pure string check with no MX or SMTP probe. Signup is a
synchronous request, a DNS lookup there is a network dependency on a path that must
not hang, and a throwaway provider's MX records are perfectly valid anyway — so the
lookup would cost latency without answering the question being asked.
"""

from disposable_email_domains import blocklist as PACKAGE_BLOCKLIST
from django.conf import settings
from rest_framework import serializers

#: Shown to the caller. Names the reason rather than saying "invalid email", because a
#: user handed "that address is not valid" for an address that demonstrably exists will
#: retype the same one.
MESSAGE = (
    "Disposable or temporary email addresses are not accepted. "
    "Please sign up with a permanent email address."
)

#: Below this the walk would be testing a bare TLD.
MIN_LABELS = 2


def _normalised(name):
    """The configured setting as a lowercase set, tolerant of stray '@' and spaces."""
    raw = getattr(settings, name, None) or ()
    return {entry.strip().lower().lstrip("@") for entry in raw if entry and entry.strip()}


def _domain_of(email):
    if not email or "@" not in email:
        return ""
    return email.rsplit("@", 1)[1].strip().lower().rstrip(".")


def _self_and_parents(domain):
    """``a.b.yopmail.com`` -> itself, ``b.yopmail.com``, ``yopmail.com``."""
    labels = [label for label in domain.split(".") if label]
    for i in range(len(labels) - MIN_LABELS + 1):
        yield ".".join(labels[i:])


def disposable_domain_for(email):
    """The blocked domain this address resolves to, or ``None`` if it is acceptable.

    Returns the *matched* domain rather than a bool so callers can log which entry
    fired — with a list this large, "why was this rejected" is otherwise unanswerable.
    """
    domain = _domain_of(email)
    if not domain:
        return None

    allowlist = _normalised("DISPOSABLE_EMAIL_ALLOWLIST")
    extra = _normalised("DISPOSABLE_EMAIL_EXTRA_BLOCKED")

    for candidate in _self_and_parents(domain):
        # Allowlist first, and at every level: an explicit allow has to beat both the
        # packaged list and a parent domain that is blocked.
        if candidate in allowlist:
            return None
        if candidate in extra or candidate in PACKAGE_BLOCKLIST:
            return candidate
    return None


def is_disposable_email(email):
    return disposable_domain_for(email) is not None


def validate_not_disposable(email):
    """Raise ``ValidationError`` for a throwaway address; return the email otherwise.

    Written to be used from ``validate_<field>``, so it returns the value it was given
    and a caller can chain it.
    """
    if is_disposable_email(email):
        raise serializers.ValidationError(MESSAGE)
    return email
