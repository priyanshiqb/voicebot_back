import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from tenant.common.models.tenant_base_model import TenantBaseModel
from tenant.models.TenantUserMapping import TenantUserMapping

#: Bytes of entropy in the raw token. 32 bytes is 256 bits, which is why the hash below
#: can be a plain SHA-256 rather than a password KDF — see `set_token`.
TOKEN_BYTES = 32


def hash_token(raw_token):
    """The stored form of an invitation token.

    A plain SHA-256, deliberately, where `AgentToken` uses Django's password hasher.
    The difference is what is being protected: a password is low-entropy and guessable,
    so it needs a slow KDF and a per-row salt, and `AgentToken` carries a separate
    plain-text prefix precisely because you cannot index a bcrypt hash.

    An invitation token is 256 bits of `secrets` output. There is nothing to brute
    force, so a slow hash buys nothing and costs an indexed lookup — this way accepting
    an invitation is one indexed query rather than a scan.

    Hashed at all, rather than stored raw, so that read access to this table does not
    hand over every live invitation.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()


class TenantInvitation(TenantBaseModel):
    """An outstanding offer of a seat at a tenant.

    The invited `CustomUser` and their `TenantUserMapping` are created **when the
    invitation is sent**, not when it is accepted. That is a seat-accounting decision:
    the `USER` allowance is counted live from live mappings, so a mapping must exist
    exactly when a seat is spent. If invitations were free until accepted, a five-seat
    tenant could send fifty and have all fifty accept, because the check would have
    passed fifty times against a count that never moved.

    So this row is not the account — it is the *pending credential* for an account that
    already exists, inactive and without a usable password. Login gates on `is_active`,
    which is what keeps an un-accepted invitation from being a way in.
    """

    email = models.EmailField(
        max_length=254,
        db_index=True,
        help_text=_(
            "The address this invitation was sent to, captured as invited. Kept "
            "separate from the user's own email so a later change to their account "
            "does not rewrite who was actually invited."
        ),
    )
    role = models.CharField(
        max_length=10,
        choices=TenantUserMapping.Role.choices,
        default=TenantUserMapping.Role.MEMBER,
        help_text=_("The role the invitee receives. OWNER is never offered here."),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tenant_invitations",
        help_text=_(
            "The inactive account created for this invitation. SET_NULL, not CASCADE: "
            "revoking hard-deletes the placeholder account so its address is not held "
            "hostage by a unique constraint, and CASCADE would take this audit record "
            "with it. `email` above is captured precisely so the record still says who "
            "was invited once the account is gone."
        ),
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_tenant_invitations",
        help_text=_("Who sent it. SET_NULL so removing them keeps the record."),
    )
    token_hash = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text=_(
            "SHA-256 of the token. The raw token exists only in the email — it is "
            "never stored, never logged, and cannot be re-sent, only reissued."
        ),
    )
    expires_at = models.DateTimeField(
        help_text=_("After this, the token is refused and the seat should be reclaimed.")
    )
    accepted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Set once, when the invitee sets their password."),
    )

    class Meta(TenantBaseModel.Meta):
        db_table = "tenant_invitation"
        verbose_name = _("Tenant Invitation")
        verbose_name_plural = _("Tenant Invitations")
        constraints = [
            # One live invitation per address per tenant. Re-inviting somebody should
            # reissue rather than accumulate rows that are all individually valid —
            # several live tokens for one seat is several ways in for one person.
            models.UniqueConstraint(
                fields=["tenant", "email"],
                condition=models.Q(is_deleted=False, accepted_at__isnull=True),
                name="uq_tenant_invitation_live_email",
            ),
        ]

    def __str__(self):
        return f"{self.email} -> {self.tenant_id} ({self.status})"

    @staticmethod
    def new_token():
        """A raw token. Returned once, to the caller, for the email. Never stored."""
        return secrets.token_urlsafe(TOKEN_BYTES)

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def is_accepted(self):
        return self.accepted_at is not None

    @property
    def status(self):
        """Derived rather than stored, so it cannot disagree with the timestamps.

        A stored status would need updating by a job the moment an invitation expired,
        and an invitation that is expired in fact but PENDING in the column is exactly
        the sort of thing that lets a dead token through.
        """
        if self.is_deleted:
            return "REVOKED"
        if self.is_accepted:
            return "ACCEPTED"
        if self.is_expired:
            return "EXPIRED"
        return "PENDING"
