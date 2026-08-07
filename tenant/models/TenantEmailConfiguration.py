from django.core.exceptions import ValidationError
from django.db import models

from tenant.common.models.tenant_base_model import TenantBaseModel


class TenantEmailConfiguration(TenantBaseModel):
    """
    A tenant's own outbound mail credentials.

    Without this, every tenant's mail leaves from one global account
    (``settings.EMAIL_HOST_USER``), which caps total send volume across the whole
    platform, mismatches the ``ORGANIZER`` we already write into booking ``.ics``
    files, and pools every tenant's sender reputation into one bucket.

    Tier 2 (audited) deliberately: who changed a tenant's mail credentials is
    exactly the kind of thing that needs ``created_by``/``updated_by``.

    **A tenant may connect many configurations, but exactly one is the default.**
    The default is the one used to reach that tenant's customers; the others sit
    ready to be promoted (a second provider to fail over to, a replacement being
    tested before cutover).

    The two halves of that rule are enforced in different places, because only one
    of them can be a database constraint:

    * **At most one default** — a partial unique index on
      ``(tenant)`` where ``is_default AND NOT is_deleted``. A race that tries to
      promote two configurations at once loses at the database, not in a check
      that happened to run first.
    * **At least one default, whenever any configuration exists** — application
      logic, since no constraint can require a row to exist. :meth:`make_default`
      and :meth:`ensure_default` own this: the first configuration a tenant
      creates becomes the default automatically, and deleting the default
      promotes another. A tenant with zero configurations has no default and
      falls back to the platform sender, which is the correct resting state.

    ``password`` is stored ENCRYPTED and must never be exposed by a serializer.
    Use :meth:`set_password` / :meth:`get_password`; never assign the column
    directly.
    """

    class Status(models.TextChoices):
        # Saved but never successfully test-sent. Must NOT be used for real mail —
        # a tenant who saves credentials and walks away keeps working mail on the
        # platform sender rather than silently dropping every message into a
        # misconfigured relay.
        UNVERIFIED = "UNVERIFIED", "Unverified"
        # A test send succeeded. The only status eligible for real mail.
        VERIFIED = "VERIFIED", "Verified"
        # A send failed permanently (bad credentials, refused sender, a host that
        # stopped resolving to a public address).
        FAILED = "FAILED", "Failed"

    class Provider(models.TextChoices):
        """Which mail service the credentials belong to.

        Drives the setup-form preset and the error messaging, so a tenant picks
        "SendGrid" and pastes one key instead of sourcing a hostname, a port, a
        TLS mode and a username convention from four places.

        This is NOT read by the send path. The preset's host/port/TLS values are
        materialized onto the columns below at save time, and only the columns are
        read afterwards — otherwise editing a preset in a future release would
        silently repoint every existing tenant's mail.
        """

        # Transactional relays — built for volume.
        SENDGRID = "SENDGRID", "SendGrid"
        SES = "SES", "Amazon SES"
        POSTMARK = "POSTMARK", "Postmark"
        MAILGUN = "MAILGUN", "Mailgun"
        BREVO = "BREVO", "Brevo"
        RESEND = "RESEND", "Resend"
        # Mailboxes for humans — low per-day send caps that vary by plan.
        GMAIL = "GMAIL", "Gmail / Google Workspace"
        ZOHO = "ZOHO", "Zoho Mail"
        HOSTINGER = "HOSTINGER", "Hostinger Email"
        OUTLOOK = "OUTLOOK", "Microsoft 365 / Outlook"
        # Anything else that speaks SMTP.
        CUSTOM = "CUSTOM", "Custom SMTP"

    name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=(
            "Label for this connection, e.g. 'SendGrid — bookings' or 'Hostinger "
            "backup'. A tenant can hold several configurations, and host alone is "
            "a poor way to tell two of them apart."
        ),
    )
    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        default=Provider.CUSTOM,
        help_text=(
            "Which mail service these credentials belong to. Fills the connection "
            "fields from a preset and selects the right field hints and error "
            "messages. Not read when sending."
        ),
    )
    is_default = models.BooleanField(
        default=False,
        help_text=(
            "Whether this is the configuration used to reach the tenant's "
            "customers. Exactly one per tenant while any configuration exists. "
            "Promote with make_default() rather than assigning this directly — a "
            "bare assignment will hit the unique index."
        ),
    )

    # --- connection ----------------------------------------------------------

    host = models.CharField(
        max_length=255,
        help_text="SMTP server hostname, e.g. smtp.sendgrid.net.",
    )
    port = models.PositiveIntegerField(
        default=587,
        help_text=(
            "SMTP port. 587 for STARTTLS, 465 for implicit TLS, 2525 where 587 is "
            "filtered. Port 25 is not accepted — cloud providers block it outbound."
        ),
    )
    username = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "SMTP login. Varies by provider — SendGrid wants the literal string "
            "'apikey', Postmark wants its server token. Blank for relays that "
            "authenticate by IP."
        ),
    )
    password = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Encrypted SMTP password or API key. Never returned by any serializer. "
            "Set via set_password(); read via get_password()."
        ),
    )
    use_tls = models.BooleanField(
        default=True,
        help_text="STARTTLS — upgrade a plaintext connection. Pairs with port 587.",
    )
    use_ssl = models.BooleanField(
        default=False,
        help_text=(
            "Implicit TLS — encrypted from the first byte. Pairs with port 465. "
            "Mutually exclusive with use_tls; Django's mail backend raises if both "
            "are set."
        ),
    )

    # --- identity ------------------------------------------------------------

    from_email = models.EmailField(
        help_text=(
            "The From: address recipients see. Most providers reject mail from an "
            "address or domain that has not been verified in their own dashboard, "
            "so a syntactically valid value here is not enough."
        ),
    )
    from_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Sender display name. Falls back to the tenant's name when blank.",
    )
    reply_to = models.EmailField(
        blank=True,
        null=True,
        help_text="Where replies go, when that is not from_email.",
    )

    # --- lifecycle -----------------------------------------------------------

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UNVERIFIED,
        db_index=True,
        help_text=(
            "Only VERIFIED configurations are used for real mail. Editing a "
            "configuration resets this to UNVERIFIED — a changed host or password "
            "invalidates the previous test."
        ),
    )
    verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When a test send first succeeded.",
    )
    last_tested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last test-send attempt, successful or not.",
    )
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Last real (non-test) send. Written at most once a minute with a "
            "targeted UPDATE — a tenant sending 500 reminders must not issue 500 "
            "writes against this row and serialize the batch on one lock."
        ),
    )
    last_error = models.TextField(
        blank=True,
        default="",
        help_text="Most recent failure, truncated. Surfaced to the owning tenant only.",
    )

    # =========================================================================
    # RESERVED — not implemented, not read, not serialized.
    #
    # Placeholders for the four non-SMTP integration approaches, added now so
    # adopting one later is a code change rather than a migration against a table
    # holding live credentials.
    #
    # RULES until an approach is actually built:
    #   * Nothing reads these. The send path is SMTP-only.
    #   * They stay out of every serializer, form, admin fieldset and API doc.
    #   * All nullable, so the columns cost nothing and constrain nothing.
    #   * `api_key` and the OAuth token columns hold SECRETS. When one of these is
    #     activated, route it through the same encryption as `password` — do not
    #     write plaintext into them because "it is only a placeholder".
    # =========================================================================

    transport = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text=(
            "RESERVED. Discriminator for which of the integration approaches below "
            "is in use. NULL means SMTP, which is the only implemented transport. "
            "Nullable so adopting another approach needs no backfill."
        ),
    )

    # 2. Provider API key — HTTPS to a known endpoint instead of SMTP. Removes the
    #    arbitrary-host problem entirely, and brings bounce/complaint webhooks.
    api_key = models.TextField(
        blank=True,
        null=True,
        help_text="RESERVED (provider API transport). Must be encrypted when used.",
    )
    api_base_url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="RESERVED (provider API transport). Region or self-hosted endpoint override.",
    )

    # 3. OAuth mailbox — "Connect Google", no password stored. The only option that
    #    works for Microsoft 365, which retired SMTP basic auth.
    oauth_account_email = models.CharField(
        max_length=254,
        blank=True,
        null=True,
        help_text="RESERVED (OAuth transport). Connected mailbox identity.",
    )
    oauth_access_token = models.TextField(
        blank=True,
        null=True,
        help_text="RESERVED (OAuth transport). Must be encrypted when used.",
    )
    oauth_refresh_token = models.TextField(
        blank=True,
        null=True,
        help_text="RESERVED (OAuth transport). Must be encrypted when used.",
    )
    oauth_token_expires_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="RESERVED (OAuth transport).",
    )
    oauth_scope = models.TextField(
        blank=True,
        null=True,
        help_text="RESERVED (OAuth transport). Granted scopes.",
    )

    # 4. Domain delegation — the tenant adds DNS records to OUR provider account.
    #    No credentials stored at all; we keep bounce handling and observability.
    sending_domain = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="RESERVED (domain delegation). The domain being delegated, e.g. acme.com.",
    )
    domain_status = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="RESERVED (domain delegation). DNS verification state.",
    )
    domain_verified_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="RESERVED (domain delegation).",
    )
    dkim_selector = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="RESERVED (domain delegation). DKIM selector issued by the provider.",
    )
    dns_records = models.JSONField(
        blank=True,
        null=True,
        help_text=(
            "RESERVED (domain delegation). The records the tenant must publish, as "
            "returned by the provider."
        ),
    )

    # 5. Display-name-only — no credentials, no DNS. Mail still leaves on platform
    #    infrastructure; only the visible sender name changes. `from_name` and
    #    `reply_to` above already carry the values; this flag is what would let the
    #    resolver apply them without treating the row as a real SMTP config.
    display_name_only = models.BooleanField(
        blank=True,
        null=True,
        help_text=(
            "RESERVED (display-name-only transport). When true, send on platform "
            "credentials but present the tenant's from_name and reply_to."
        ),
    )

    class Meta(TenantBaseModel.Meta):
        db_table = "tenant_email_configurations"
        verbose_name = "Tenant Email Configuration"
        verbose_name_plural = "Tenant Email Configurations"
        ordering = ["-created_at"]
        constraints = [
            # At most one default per tenant. Partial, so soft-deleted rows and
            # non-default rows do not participate — a tenant can hold as many
            # non-default configurations as they like.
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(is_default=True, is_deleted=False),
                name="unique_default_tenant_email_configuration",
            ),
            # Two connections called "SendGrid" in one list help nobody. Blank
            # names are excluded so an unnamed configuration is still allowed —
            # and more than one of them.
            models.UniqueConstraint(
                fields=["tenant", "name"],
                condition=models.Q(is_deleted=False) & ~models.Q(name=""),
                name="unique_tenant_email_configuration_name",
            ),
        ]

    # --- the single-default invariant -----------------------------------------

    def make_default(self):
        """Promote this configuration to the tenant's default.

        Clearing the previous default and setting this one must happen in that
        order and inside one transaction. Reversed, both rows are momentarily
        ``is_default=True`` and the partial unique index rejects the write; split
        across two transactions, a crash between them leaves the tenant with no
        default at all — which silently reverts their customer mail to the
        platform sender.
        """
        from django.db import transaction

        with transaction.atomic():
            type(self).objects.filter(
                tenant_id=self.tenant_id, is_default=True
            ).exclude(pk=self.pk).update(is_default=False)
            if not self.is_default:
                self.is_default = True
                self.save(update_fields=["is_default", "updated_at"])
        return self

    @classmethod
    def ensure_default(cls, tenant_id):
        """Guarantee a tenant's default is present *and usable*.

        Call after anything that can remove a default from service: delete,
        disconnect, or an admin edit. Losing a usable default is not a visible
        failure — mail keeps flowing on the platform sender, from the wrong
        address, until somebody notices.

        "Usable" means connected. A disconnected default satisfies the database
        constraint perfectly well and still sends nothing, so a check that only
        asked "does a default row exist?" would report health while the tenant's
        customer mail quietly reverted to our address.

        Returns the resulting default, or ``None`` when the tenant has no
        configurations at all — the correct resting state, not an error.
        """
        existing = cls.objects.filter(tenant_id=tenant_id)
        current = existing.filter(is_default=True).first()
        if current is not None and current.is_active:
            return current

        # A verified configuration beats an unverified one: promoting something
        # that has never completed a test send hands the tenant a default that
        # cannot actually send, and the resolver would skip straight back to the
        # platform sender.
        replacement = (
            existing.filter(status=cls.Status.VERIFIED, is_active=True).first()
            or existing.filter(is_active=True).first()
        )
        if replacement is not None:
            return replacement.make_default()

        # Nothing connected to promote. Keep the disconnected default rather than
        # demoting it: it is still the tenant's stated choice, reconnecting it
        # restores service with no further action, and demoting would leave the
        # row set with no default at all.
        if current is not None:
            return current

        fallback = existing.first()
        return fallback.make_default() if fallback else None

    def is_booking_sender(self):
        """Whether this configuration is the one assigned to carry booking mail.

        Nothing is blocked on this. Disconnecting or deleting an assigned
        configuration clears the assignment and proceeds — see
        :meth:`release_from_settings`. The predicate is kept so a caller can *warn*
        before doing that (booking mail has no platform fallback, so it stops
        rather than rerouting), which is a better place for the friction than a
        refusal the tenant has to work around.
        """
        from tenant.models.TenantSettings import TenantSettings

        return TenantSettings.objects.filter(
            tenant_id=self.tenant_id, booking_email_configuration_id=self.pk
        ).exists()

    def release_from_settings(self):
        """Drop any assignment pointing at this configuration.

        ``on_delete=SET_NULL`` never fires for us: rows are soft-deleted, so the
        database sees no delete and the pointer survives as a dangling id. Reads go
        through the soft-delete-aware manager and so return ``None`` anyway, but
        leaving the stale id in place makes the settings row lie about what is
        configured.

        Called by :meth:`disconnect` and :meth:`soft_delete` rather than by the
        viewset alone, so every path — bulk delete, the admin, a shell session —
        leaves the settings row honest.
        """
        from tenant.models.TenantSettings import TenantSettings

        TenantSettings.objects.filter(
            tenant_id=self.tenant_id, booking_email_configuration_id=self.pk
        ).update(booking_email_configuration=None)

    def soft_delete(self, user=None):
        """Delete, releasing the booking assignment first.

        Deleting an assigned sender is allowed and **stops booking email** — there
        is no platform fallback to inherit it. Clearing the pointer is what keeps
        that state readable: a settings row still naming a deleted configuration
        reports a sender that cannot send, which is indistinguishable from a
        misconfiguration when someone comes to diagnose the silence.
        """
        from django.db import transaction

        with transaction.atomic():
            self.release_from_settings()
            super().soft_delete(user=user)

    # --- connect / disconnect -------------------------------------------------

    def disconnect(self):
        """Stop using this configuration, without discarding its credentials.

        Distinct from deletion on purpose: a tenant pausing a provider — an
        expired card, a suspected deliverability problem, a migration in
        progress — should not have to re-enter an API key to come back.

        Promotes a connected replacement when this was the default, so
        disconnecting never silently drops the tenant onto the platform sender
        while a working alternative sits unused.

        **Releases the booking assignment if this held it.** Disconnecting the
        assigned sender is allowed, and stops booking email — there is no platform
        fallback. Clearing the pointer rather than leaving it aimed at an inactive
        configuration is what makes that state legible afterwards: ``will_send``
        then reports *nothing assigned* instead of naming a sender that cannot
        send.

        **A VERIFIED configuration drops to UNVERIFIED here.** Verification is a
        claim about a specific moment — "these credentials worked when we tried
        them" — and the reasons a configuration gets disconnected are largely the
        reasons that claim expires: a rotated password, a revoked app key, a
        provider that suspended the account. Carrying VERIFIED across an
        open-ended pause would let a configuration be reassigned as the booking
        sender months later on the strength of a test that predates whatever
        caused the disconnect, and the first customer to find out would be the
        one whose confirmation never arrived. Re-testing is one request.

        FAILED is deliberately left alone. Rewriting it to UNVERIFIED would
        discard the more specific fact — that a send was actually refused — in
        favour of a vaguer one.
        """
        from django.db import transaction

        with transaction.atomic():
            self.release_from_settings()

            changed = []

            if self.is_active:
                self.is_active = False
                changed.append("is_active")

            if self.status == self.Status.VERIFIED:
                self.status = self.Status.UNVERIFIED
                # Cleared together with the status, matching how the serializer
                # represents "unverified" after a credential edit. A verified_at
                # left standing under an UNVERIFIED status reads as a working
                # sender to anything that checks the timestamp instead of the flag.
                self.verified_at = None
                changed.extend(["status", "verified_at"])

            if changed:
                self.save(update_fields=changed + ["updated_at"])

            return type(self).ensure_default(self.tenant_id)

    def reconnect(self):
        """Put this configuration back into service.

        If the tenant's default is currently disconnected, this becomes the
        default — reconnecting your only working sender should not also require
        remembering to promote it. An already-connected default is left alone.

        **Does not restore VERIFIED.** ``disconnect`` drops it, and only a passing
        test send sets it — that asymmetry is the point. A configuration comes
        back in service but stays ineligible to carry booking mail until it has
        proved it still works, so reconnecting cannot quietly re-arm a sender
        whose credentials expired during the pause.
        """
        from django.db import transaction

        with transaction.atomic():
            if not self.is_active:
                self.is_active = True
                self.save(update_fields=["is_active", "updated_at"])
            type(self).ensure_default(self.tenant_id)
            self.refresh_from_db()
            return self

    # --- encrypted password ---------------------------------------------------

    def set_password(self, raw):
        """Encrypt and store an SMTP password. Blank/None clears it.

        Imported lazily: the crypto helper lives in ``core`` while this model
        lives in ``tenant``, and a module-level import would invert the app
        dependency. Same pattern as ``CalendarIntegration.set_tokens``.
        """
        from core.services import crypto

        self.password = crypto.encrypt(raw) if raw else ""

    def get_password(self):
        """Decrypt the stored SMTP password. Returns "" when unset."""
        from core.services import crypto

        return crypto.decrypt(self.password)

    def has_password(self):
        """Whether a password is stored — without revealing it or its length."""
        return bool(self.password)

    # --- validation -----------------------------------------------------------

    def clean(self):
        """Reject the one combination Django's mail backend blows up on.

        ``EmailBackend.__init__`` raises ``ValueError`` when both flags are set,
        which escapes as a 500 from deep inside the send path rather than as a
        field error anyone can act on.
        """
        super().clean()
        if self.use_tls and self.use_ssl:
            raise ValidationError(
                {
                    "use_ssl": (
                        "use_tls and use_ssl are mutually exclusive. Use STARTTLS "
                        "(use_tls) on port 587, or implicit TLS (use_ssl) on port 465."
                    )
                }
            )

    def __str__(self):
        label = self.name or f"{self.host}:{self.port}"
        suffix = " [default]" if self.is_default else ""
        return f"{label} ({self.status}){suffix}"
