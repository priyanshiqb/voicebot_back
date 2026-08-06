"""
SMTP connection presets, keyed by ``TenantEmailConfiguration.Provider``.

Plain data, no database table. Most support tickets on a feature like this are one
of two things: someone putting their SendGrid *login* in the username field, or
someone guessing a port. A preset with per-provider field hints removes both.

**The preset is a form default, not a runtime lookup.** On save, the chosen
preset's host/port/TLS values are materialised onto the row, and the send path
reads only the columns. Resolving the preset at send time would mean editing this
dict in a future release silently repointed every existing tenant's mail — a
configuration that worked on Tuesday failing on Wednesday with no change the
tenant can see.
"""

# Transactional relays are built for volume. Mailboxes are built for a human
# reading their inbox, and cap daily sends well below anything a booking or
# campaign workload needs.
KIND_RELAY = "RELAY"
KIND_MAILBOX = "MAILBOX"

# Deliberately free of specific numbers: caps differ per plan, change without
# notice, and a stale figure in our UI is worse than an honest "check your plan".
MAILBOX_WARNING = (
    "This is a personal or business mailbox, not a bulk sending service. Your "
    "provider limits how many messages you can send per day, and that limit "
    "varies by plan. For campaigns or high booking volume, use a transactional "
    "provider such as SendGrid, Amazon SES, or Postmark."
)

# Microsoft retired basic authentication for SMTP submission in Exchange Online in
# September 2025. A mailbox username and password cannot authenticate; OAuth2 is
# required, which this feature does not provide. The preset stays listed so the
# failure is explained rather than mysterious — see `smtp_failure_hint`.
MICROSOFT_BASIC_AUTH_WARNING = (
    "Microsoft 365 no longer accepts a username and password for SMTP (basic "
    "authentication was retired in September 2025). Use a transactional provider "
    "such as SendGrid, Amazon SES, or Postmark, or configure a certificate-based "
    "Exchange connector."
)

# Hosts that speak Microsoft's submission endpoint. Matched by suffix on the
# *host*, not on `provider`, so a CUSTOM configuration pointed at Office 365 gets
# the same explanation as one that picked OUTLOOK from the dropdown.
MICROSOFT_HOST_SUFFIXES = ("office365.com", "outlook.com", "outlook.office365.com")


PRESETS = {
    "SENDGRID": {
        "label": "SendGrid",
        "kind": KIND_RELAY,
        "host": "smtp.sendgrid.net",
        "port": 587,
        "use_tls": True,
        "use_ssl": False,
        "host_required": False,
        "username_hint": "Enter the literal word: apikey",
        "password_hint": "Your SendGrid API key",
        "warning": None,
    },
    "SES": {
        "label": "Amazon SES",
        "kind": KIND_RELAY,
        # Region-dependent. Guessing us-east-1 would silently send through the
        # wrong region, so the tenant must supply it.
        "host": None,
        "port": 587,
        "use_tls": True,
        "use_ssl": False,
        "host_required": True,
        "username_hint": "Your SES SMTP username (not an AWS access key ID)",
        "password_hint": "Your SES SMTP password, generated in the SES console",
        "warning": (
            "Enter your regional endpoint as the host, e.g. "
            "email-smtp.ap-south-1.amazonaws.com. If your SES account is still in "
            "sandbox mode this test will succeed, but mail to your customers will "
            "not be delivered."
        ),
    },
    "POSTMARK": {
        "label": "Postmark",
        "kind": KIND_RELAY,
        "host": "smtp.postmarkapp.com",
        "port": 587,
        "use_tls": True,
        "use_ssl": False,
        "host_required": False,
        "username_hint": "Your Server API token",
        "password_hint": "The same Server API token again",
        "warning": None,
    },
    "MAILGUN": {
        "label": "Mailgun",
        "kind": KIND_RELAY,
        "host": "smtp.mailgun.org",
        "port": 587,
        "use_tls": True,
        "use_ssl": False,
        "host_required": False,
        "username_hint": "postmaster@your-domain.com",
        "password_hint": "Your Mailgun SMTP password",
        "warning": None,
    },
    "BREVO": {
        "label": "Brevo",
        "kind": KIND_RELAY,
        "host": "smtp-relay.brevo.com",
        "port": 587,
        "use_tls": True,
        "use_ssl": False,
        "host_required": False,
        "username_hint": "Your Brevo account login email",
        "password_hint": "Your Brevo SMTP key",
        "warning": None,
    },
    "RESEND": {
        "label": "Resend",
        "kind": KIND_RELAY,
        "host": "smtp.resend.com",
        "port": 587,
        "use_tls": True,
        "use_ssl": False,
        "host_required": False,
        "username_hint": "Enter the literal word: resend",
        "password_hint": "Your Resend API key",
        "warning": None,
    },
    "GMAIL": {
        "label": "Gmail / Google Workspace",
        "kind": KIND_MAILBOX,
        "host": "smtp.gmail.com",
        "port": 587,
        "use_tls": True,
        "use_ssl": False,
        "host_required": False,
        "username_hint": "Your full Gmail address",
        "password_hint": "An App Password — your normal password will not work",
        "warning": MAILBOX_WARNING,
    },
    "ZOHO": {
        "label": "Zoho Mail",
        "kind": KIND_MAILBOX,
        "host": "smtp.zoho.com",
        "port": 465,
        "use_tls": False,
        "use_ssl": True,
        "host_required": False,
        "username_hint": "Your full Zoho mailbox address",
        "password_hint": "An app password generated in Zoho",
        "warning": MAILBOX_WARNING,
    },
    "HOSTINGER": {
        "label": "Hostinger Email",
        "kind": KIND_MAILBOX,
        "host": "smtp.hostinger.com",
        # Hostinger's own setup pages lead with 465/SSL, so a tenant copying from
        # those pages finds the form already matching. 587 with STARTTLS also
        # works — switching means flipping both flags, which is why the form
        # should present encryption as one choice rather than two booleans.
        "port": 465,
        "use_tls": False,
        "use_ssl": True,
        "host_required": False,
        "username_hint": "Your full email address, e.g. bookings@yourdomain.com",
        "password_hint": "Your mailbox password",
        "warning": MAILBOX_WARNING,
    },
    "OUTLOOK": {
        "label": "Microsoft 365 / Outlook",
        "kind": KIND_MAILBOX,
        "host": "smtp.office365.com",
        "port": 587,
        "use_tls": True,
        "use_ssl": False,
        "host_required": False,
        "username_hint": "Your mailbox address",
        "password_hint": "Your mailbox password",
        "warning": MICROSOFT_BASIC_AUTH_WARNING,
    },
    "CUSTOM": {
        "label": "Custom SMTP",
        "kind": None,
        "host": None,
        "port": 587,
        "use_tls": True,
        "use_ssl": False,
        "host_required": True,
        "username_hint": "Your SMTP username",
        "password_hint": "Your SMTP password",
        "warning": None,
    },
}

# Fields a preset may materialise onto a configuration row. Credentials are
# deliberately absent — a preset never supplies a username or password.
CONNECTION_FIELDS = ("host", "port", "use_tls", "use_ssl")


def get_preset(provider):
    """Return the preset for a provider, or the CUSTOM preset if unrecognised."""
    return PRESETS.get(provider) or PRESETS["CUSTOM"]


def host_is_required(provider):
    return get_preset(provider)["host_required"]


def is_microsoft_host(host):
    """Whether a host is Microsoft's SMTP submission endpoint.

    Keyed on host rather than on the stored provider so a CUSTOM configuration
    pointed at Office 365 gets the same explanation as one that picked OUTLOOK.
    """
    host = (host or "").strip().lower().rstrip(".")
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in MICROSOFT_HOST_SUFFIXES)


def catalogue():
    """The full preset list, shaped for the setup form's provider dropdown.

    Returned by the API so the frontend is not a hardcoded copy of a dict that
    lives here — the `username_hint` in particular is what stops a tenant putting
    their SendGrid login where the literal string `apikey` belongs.
    """
    return [
        {
            "value": value,
            "label": preset["label"],
            "kind": preset["kind"],
            "host": preset["host"],
            "port": preset["port"],
            "use_tls": preset["use_tls"],
            "use_ssl": preset["use_ssl"],
            "host_required": preset["host_required"],
            "username_hint": preset["username_hint"],
            "password_hint": preset["password_hint"],
            "warning": preset["warning"],
        }
        for value, preset in PRESETS.items()
    ]
