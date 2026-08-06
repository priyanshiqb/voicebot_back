"""
Building an SMTP connection from a stored ``TenantEmailConfiguration``, and the
test send that verifies one.

Deliberately NOT the resolver. Nothing here decides *which* configuration to use
for a given tenant's mail — that arrives with the mail service, and until then no
product code sends through a tenant's credentials. These functions only act on a
configuration handed to them explicitly, which is exactly what a test send is.
"""

import logging
import smtplib

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.exceptions import ValidationError

from utils.mail import providers, validators

logger = logging.getLogger(__name__)

SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

# Django's SMTP backend defaults to timeout=None, which blocks forever. A host
# that accepts the TCP connection and then goes silent would hang a worker
# permanently, so this is passed on every connection, never left to the default.
DEFAULT_TIMEOUT_SECONDS = 10

TEST_SUBJECT = "Your email configuration is working"
TEST_BODY = (
    "This is a test message confirming that your outbound email settings are "
    "working.\n\nIf you did not request this, you can ignore it."
)


def timeout_seconds():
    return int(getattr(settings, "TENANT_SMTP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))


def format_from(config):
    """The ``From:`` header: display name plus address.

    Falls back to the tenant's own name, so a configuration saved without a
    display name still shows something a recipient recognises rather than a bare
    address.
    """
    name = (config.from_name or "").strip() or getattr(config.tenant, "name", "").strip()
    return f"{name} <{config.from_email}>" if name else config.from_email


def build_connection(config):
    """An SMTP connection for this configuration.

    Re-validates the host first. The configuration passed validation when it was
    saved, but a hostname is a moving target — this is the check that survives DNS
    rebinding.
    """
    validators.validate_host(config.host)
    return get_connection(
        backend=SMTP_BACKEND,
        host=config.host,
        port=config.port,
        username=config.username or None,
        password=config.get_password() or None,
        use_tls=config.use_tls,
        use_ssl=config.use_ssl,
        timeout=timeout_seconds(),
        fail_silently=False,
    )


def describe_failure(config, exc):
    """A message the owning tenant can act on.

    Generic copy ("Configuration test failed") is worthless here: the difference
    between 'authentication failed' and 'connection refused' is the entire
    diagnostic value, and it goes only to the tenant that owns the credentials, so
    it discloses nothing.
    """
    if isinstance(exc, ValidationError):
        messages = getattr(exc, "message_dict", None)
        if messages:
            return "; ".join(m for msgs in messages.values() for m in msgs)
        return "; ".join(exc.messages)

    if isinstance(exc, smtplib.SMTPAuthenticationError):
        if providers.is_microsoft_host(config.host):
            return providers.MICROSOFT_BASIC_AUTH_WARNING
        return (
            "The mail server rejected these credentials. Check the username and "
            "password — several providers expect a fixed username (SendGrid uses "
            "the literal word 'apikey') rather than your account login."
        )

    if isinstance(exc, smtplib.SMTPSenderRefused):
        return (
            f"The mail server refused '{config.from_email}' as a sender. Most "
            "providers require the address or its domain to be verified in their "
            "dashboard before it can send."
        )

    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The mail server refused the recipient address."

    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return (
            "The mail server does not support the requested authentication "
            "method. If this is Microsoft 365, password-based SMTP is no longer "
            "available."
        )

    if isinstance(exc, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError)):
        return (
            f"Could not connect to {config.host}:{config.port}. Check the host, "
            "the port, and whether encryption should be STARTTLS or SSL."
        )

    return str(exc) or "The mail server rejected the message."


def is_permanent(exc):
    """Whether a failure means the configuration itself is broken.

    A refused recipient is about one address, not the credentials, so it must not
    mark a working configuration failed. A dropped connection is transient and
    belongs to the caller's retry policy.
    """
    return isinstance(
        exc,
        (
            ValidationError,
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPNotSupportedError,
        ),
    )


def send_test_email(config, recipient):
    """Send a verification message through this configuration.

    Returns ``(ok, error_message)``. Never raises — the caller renders the outcome
    into a response and persists it onto the configuration.

    Runs synchronously rather than through Celery: a human is waiting on a form,
    and the connection timeout already bounds it.
    """
    try:
        connection = build_connection(config)
        message = EmailMultiAlternatives(
            subject=TEST_SUBJECT,
            body=TEST_BODY,
            from_email=format_from(config),
            to=[recipient],
            reply_to=[config.reply_to] if config.reply_to else None,
            connection=connection,
        )
        message.send()
        return True, ""
    except Exception as exc:  # noqa: BLE001 — every failure is reported, not raised
        logger.warning(
            "SMTP test failed for configuration %s (%s:%s): %s",
            config.pk,
            config.host,
            config.port,
            exc,
        )
        return False, describe_failure(config, exc)
