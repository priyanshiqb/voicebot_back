"""
Outbound mail support for tenant-owned SMTP credentials.

What exists here today is only what the configuration API needs: the provider
preset catalogue, connection validation, and a synchronous test send.

**Nothing in the product sends through a tenant's credentials yet.** The resolver
that decides which sender a given tenant's mail should use — and the send entry
point every app will call — is a separate step. Until it lands, every existing
email path still uses the platform credentials in ``settings.EMAIL_*``, unchanged.
"""

from utils.mail import connection, providers, validators

__all__ = ["connection", "providers", "validators"]
