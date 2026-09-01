"""
Inviting somebody to a tenant, and letting them in.

Two rules shape everything here.

**A seat is spent when the invitation is sent.** The `USER` allowance counts live
`TenantUserMapping` rows, so the mapping is created up front and released on revoke or
expiry. Anything else lets a tenant issue more invitations than it has seats and
discover the problem only when people start accepting.

**Sending an invitation never reveals whether the address already has an account.**
`CustomUser.email` is unique platform-wide, so the obvious implementation returns "user
with this email already exists" — which tells the inviter that address is registered
*somewhere on the platform*, possibly at a competitor. The caller gets the same answer
either way; only the email differs.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from tenant.models.TenantInvitation import TenantInvitation, hash_token
from tenant.models.TenantUserMapping import TenantUserMapping
from tenant.services import membership

logger = logging.getLogger(__name__)

DEFAULT_EXPIRY_DAYS = 7


def expiry_days():
    return int(getattr(settings, "TENANT_INVITATION_EXPIRY_DAYS", DEFAULT_EXPIRY_DAYS))


def _existing_user(email):
    """Any account holding this address, soft-deleted ones included.

    Not filtered on ``is_deleted``, deliberately. ``CustomUser.email`` is unique at the
    database level and a soft delete is a flag write, so a soft-deleted row still owns
    its address — filtering it out here would let this function say "free" and then
    fail with an IntegrityError one line later.

    The consequence is that a previously removed employee cannot simply be re-invited;
    their account has to be restored instead. That is the honest behaviour given the
    unique constraint, and better than a 500.
    """
    from user.models.CustomUser import CustomUser

    return CustomUser.objects.filter(email__iexact=email).first()


@transaction.atomic
def create_invitation(*, tenant, email, role, invited_by, seat_check):
    """Create the placeholder account, the mapping, and the invitation.

    Returns ``(invitation, raw_token)`` — or ``(None, None)`` when the address already
    belongs to somebody, which is not an error the caller may distinguish. See the
    module docstring.

    ``seat_check`` is passed in rather than imported so the entitlement guard stays on
    the viewset that owns it; this service is also reachable from a management command
    where a plan limit is not the right gate.
    """
    from user.models.CustomUser import CustomUser

    email = email.strip().lower()

    if role == TenantUserMapping.Role.OWNER:
        # Not a 403: the API never offers OWNER, so reaching here means a caller
        # constructed it, and the honest answer is that it is not a valid choice.
        raise ValidationError({"role": "An invitation cannot grant ownership."})

    # Before the "already registered" check, and the order is load-bearing. The first
    # invitation to an address creates a placeholder account holding it, so checking
    # first would see that placeholder, conclude the address was taken, and make
    # re-inviting somebody impossible — while leaving their original token live.
    #
    # Revoking first clears both the invitation and the placeholder, so the address is
    # genuinely free by the time it is tested. Re-inviting therefore replaces rather
    # than accumulates: several live tokens for one seat would be several ways in for
    # one person.
    revoke_live_invitations(tenant=tenant, email=email, revoked_by=invited_by)

    if _existing_user(email) is not None:
        # Deliberately not an error. The caller is told nothing; the mail differs.
        # Reached when the address belongs to a real account, or to a pending
        # invitation at a *different* tenant — which is not ours to revoke.
        logger.info(
            "tenant %s: invitation to %s skipped, address already registered",
            tenant.id,
            email,
        )
        return None, None

    seat_check()

    user = CustomUser.objects.create_user(email=email, password=None)
    # Explicit, not merely the default: an invited account must not be a way in before
    # it is accepted, and login gates on exactly this.
    user.is_active = False
    user.set_unusable_password()
    user.save()

    TenantUserMapping.objects.create(
        user=user,
        tenant=tenant,
        role=role,
        created_by=invited_by,
        updated_by=invited_by,
    )

    raw_token = TenantInvitation.new_token()
    invitation = TenantInvitation.objects.create(
        tenant=tenant,
        email=email,
        role=role,
        user=user,
        invited_by=invited_by,
        token_hash=hash_token(raw_token),
        expires_at=timezone.now() + timedelta(days=expiry_days()),
        created_by=invited_by,
        updated_by=invited_by,
    )

    # Returned, never stored and never logged. If this value is lost the invitation
    # must be reissued, which is the correct trade for not keeping live tokens around.
    return invitation, raw_token


def find_by_token(raw_token):
    """The live invitation for this token, or None.

    One indexed lookup on the hash. Every rejection reason collapses to None on
    purpose — an endpoint that distinguishes "unknown" from "expired" from "already
    used" is an oracle for guessing which tokens exist.
    """
    if not raw_token:
        return None

    invitation = (
        TenantInvitation.objects.filter(
            token_hash=hash_token(raw_token), is_deleted=False
        )
        .select_related("tenant", "user")
        .first()
    )
    if invitation is None:
        return None
    if invitation.is_accepted or invitation.is_expired:
        return None
    return invitation


@transaction.atomic
def accept(*, raw_token, password):
    """Turn an invitation into a usable account. Returns the user.

    Raises ``ValidationError`` with a single, uniform message for every failure, so the
    endpoint cannot be used to learn which tokens are real.
    """
    invitation = find_by_token(raw_token)
    if invitation is None:
        raise ValidationError(
            {"token": "This invitation link is not valid or has expired."}
        )

    # Re-read under a lock: two clicks on the same link must not both succeed and
    # neither should race the revoke path.
    locked = (
        TenantInvitation.objects.select_for_update()
        .filter(pk=invitation.pk, is_deleted=False, accepted_at__isnull=True)
        .first()
    )
    if locked is None or locked.is_expired:
        raise ValidationError(
            {"token": "This invitation link is not valid or has expired."}
        )

    user = locked.user
    user.set_password(password)
    user.is_active = True
    user.save(update_fields=["password", "is_active", "updated_at"])

    locked.accepted_at = timezone.now()
    locked.save(update_fields=["accepted_at", "updated_at"])

    logger.info(
        "tenant %s: invitation accepted by user %s", locked.tenant_id, user.pk
    )
    return user


@transaction.atomic
def revoke(invitation, revoked_by=None):
    """Withdraw an unaccepted invitation and give the seat back.

    An accepted invitation is left alone: by then the row is only a record of how
    somebody joined, and the way to remove them is to remove the user.
    """
    if invitation.is_accepted:
        raise ValidationError(
            {"detail": "That invitation has already been accepted. Remove the user instead."}
        )

    user = invitation.user

    if user is not None:
        membership.release_seat(user, deleted_by=revoked_by)

    invitation.soft_delete(user=revoked_by)

    if user is not None:
        # The placeholder account goes too, and it goes *hard*. A soft delete would
        # leave the row owning its address at the database level — `email` is
        # `unique=True` and a flag write does not release a unique constraint — so the
        # same person could never be invited again.
        #
        # Guarded, because "hard delete a user" must never fire on a real account: only
        # something that has never been active and has no usable password can be a
        # placeholder. Anything else is soft-deleted, and its seat is already released.
        if not user.is_active and not user.has_usable_password():
            user.delete()
        else:
            user.soft_delete(user=revoked_by)

    logger.info(
        "tenant %s: invitation to %s revoked", invitation.tenant_id, invitation.email
    )
    return invitation


def revoke_live_invitations(*, tenant, email, revoked_by=None):
    """Revoke any outstanding invitations for this address at this tenant."""
    live = TenantInvitation.objects.filter(
        tenant=tenant,
        email__iexact=email,
        is_deleted=False,
        accepted_at__isnull=True,
    ).select_related("user")

    for invitation in live:
        revoke(invitation, revoked_by=revoked_by)


def sweep_expired(tenant_id=None):
    """Release seats held by invitations nobody accepted. Returns how many.

    Not on a beat: an expired invitation is already refused at acceptance, so this is
    housekeeping for the seat count rather than a correctness gate, and a tenant can
    always revoke by hand. Run it from a management command until there is evidence it
    needs to be automatic.
    """
    queryset = TenantInvitation.objects.filter(
        is_deleted=False,
        accepted_at__isnull=True,
        expires_at__lt=timezone.now(),
    ).select_related("user")
    if tenant_id:
        queryset = queryset.filter(tenant_id=tenant_id)

    released = 0
    for invitation in queryset:
        revoke(invitation)
        released += 1
    return released
