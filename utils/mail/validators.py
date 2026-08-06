"""
Validation for tenant-supplied SMTP connection details.

A tenant gives us a hostname and a port, and our worker dials it. That is SSRF by
design: without the checks here, any tenant user can point `host` at
169.254.169.254 (cloud metadata), 127.0.0.1:6379 (our Redis), or an internal
service, and use our infrastructure as a probe.

Raises ``django.core.exceptions.ValidationError`` with a field-keyed message dict,
so the same functions serve the serializer, model ``clean()``, and the send path.
The serializer is responsible for translating that into a DRF error.
"""

import ipaddress
import socket

from django.conf import settings
from django.core.exceptions import ValidationError

# 25 is deliberately absent. AWS, GCP and Azure all block outbound port 25 by
# default, so allowing it buys a connection timeout and a confusing "connection
# failed" instead of an immediate, accurate field error. No provider needs 25 for
# authenticated submission: 587 is STARTTLS submission, 465 is implicit TLS, and
# 2525 is the common fallback where 587 is filtered.
DEFAULT_ALLOWED_PORTS = (465, 587, 2525)


def allowed_ports():
    return tuple(getattr(settings, "TENANT_SMTP_ALLOWED_PORTS", DEFAULT_ALLOWED_PORTS))


def _allow_private_hosts():
    """Whether non-public hosts are permitted.

    True only for local development against MailHog/Mailpit on localhost, which
    the rules below would otherwise block. Production must pin this False in
    settings rather than leaving it env-controlled — a control that can be flipped
    by environment in production is not a control.
    """
    return bool(getattr(settings, "TENANT_SMTP_ALLOW_PRIVATE_HOSTS", False))


def _is_public(address):
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_port(port):
    permitted = allowed_ports()
    if port not in permitted:
        raise ValidationError(
            {
                "port": (
                    f"Port {port} is not allowed. Use one of: "
                    f"{', '.join(str(p) for p in permitted)}."
                )
            }
        )


def validate_tls_flags(use_tls, use_ssl):
    """Reject the combinations Django's SMTP backend cannot honour.

    ``EmailBackend.__init__`` raises ``ValueError`` when both flags are set, which
    escapes as a 500 from deep inside the send path rather than as a field error
    anyone can act on.
    """
    if use_tls and use_ssl:
        raise ValidationError(
            {
                "use_ssl": (
                    "Use STARTTLS (use_tls) on port 587, or implicit TLS (use_ssl) "
                    "on port 465 — not both."
                )
            }
        )
    if not use_tls and not use_ssl and not _allow_private_hosts():
        raise ValidationError(
            {
                "use_tls": (
                    "Encryption is required. Plaintext SMTP sends your password "
                    "across the internet in the clear."
                )
            }
        )


def validate_host(host):
    """Reject hosts that do not resolve to a public address.

    Called both when a configuration is saved **and** immediately before every
    connection. Save-time-only validation is defeated by DNS rebinding: point a
    domain at a public IP, pass validation, then repoint it at 127.0.0.1. The
    send-time call is the one that actually protects anything.
    """
    host = (host or "").strip()
    if not host:
        raise ValidationError({"host": "A hostname is required."})

    if _allow_private_hosts():
        return

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # A host we cannot resolve is a host we should not dial. Failing closed
        # also removes the "unresolvable now, resolvable later" bypass.
        raise ValidationError(
            {"host": f"'{host}' could not be resolved. Check the hostname."}
        )

    for info in infos:
        address = info[4][0]
        try:
            public = _is_public(address)
        except ValueError:
            public = False
        if not public:
            raise ValidationError(
                {
                    "host": (
                        f"'{host}' resolves to a private or reserved address "
                        f"({address}) and cannot be used as a mail server."
                    )
                }
            )


def validate_connection(*, host, port, use_tls, use_ssl):
    """Every connection-level check, in one call.

    Ordered cheapest-first so an obviously wrong port fails without paying for a
    DNS lookup.
    """
    validate_port(port)
    validate_tls_flags(use_tls, use_ssl)
    validate_host(host)
