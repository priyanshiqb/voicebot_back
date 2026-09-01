"""
Who at a tenant may act on whom.

One module, because "may I touch this person" is a question the user viewset, a future
invitation endpoint, and the admin will all ask, and three copies of it will drift. The
rules are deliberately small enough to state in full:

* A member may act only on themselves.
* An admin may act on themselves and on any member or admin — but never on the owner.
* The owner may act on anyone, including themselves.

Everything here reads ``TenantUserMapping.role``. Nothing reads ``CustomUser.UserType``,
which is about what kind of *account* something is (machine, platform staff) and is
explicitly not a tenant role — a tenant's own administrator must never reach platform
operations.
"""

import logging

from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from tenant.models.TenantUserMapping import TenantUserMapping

logger = logging.getLogger(__name__)


def mapping_for(user):
    """The user's tenant mapping, or None.

    None means "not a member of any tenant", which is the state of platform staff and
    machine accounts. Callers must treat it as "no authority here" rather than as an
    error — a superuser is handled separately and earlier, by `is_superuser`.
    """
    if user is None or not user.is_authenticated:
        return None
    return (
        TenantUserMapping.objects.filter(user=user, is_deleted=False)
        .select_related("tenant")
        .first()
    )


def role_of(user):
    mapping = mapping_for(user)
    return mapping.role if mapping else None


def can_manage_members(user):
    """Whether this user may add, remove, or edit other people at their tenant."""
    if getattr(user, "is_superuser", False):
        return True
    return role_of(user) in TenantUserMapping.MANAGER_ROLES


def can_act_on(actor, target):
    """Whether `actor` may modify or remove `target`. Both are CustomUsers.

    Order matters here. Self-service comes first so a member can always maintain their
    own record, and the owner check comes before the manager check so an admin cannot
    reach the owner by being a manager.
    """
    if getattr(actor, "is_superuser", False):
        return True

    if actor.pk == target.pk:
        return True

    actor_mapping = mapping_for(actor)
    target_mapping = mapping_for(target)

    if actor_mapping is None or target_mapping is None:
        return False

    # Cross-tenant is already a 404 from the viewset's queryset. Repeated here because
    # this service is also reachable from places that do not filter first, and a
    # permission helper that assumes it was called safely is one that eventually is not.
    if actor_mapping.tenant_id != target_mapping.tenant_id:
        return False

    if actor_mapping.role not in TenantUserMapping.MANAGER_ROLES:
        return False

    # The owner is untouchable by anyone else, admins included. Without this an admin
    # can remove the person holding the billing relationship, and nothing can undo it
    # from inside the tenant.
    if target_mapping.role == TenantUserMapping.Role.OWNER:
        return False

    return True


def assert_can_manage_members(user):
    if not can_manage_members(user):
        raise PermissionDenied(
            "Only an owner or admin can manage the people at this organisation."
        )


def assert_can_act_on(actor, target):
    if can_act_on(actor, target):
        return
    # Deliberately not "that person is the owner": the message is the same whether the
    # target is out of reach or the actor simply lacks the role, so it cannot be used
    # to probe who the owner is.
    raise PermissionDenied("You do not have permission to modify this user.")


@transaction.atomic
def transfer_ownership(current_owner, new_owner):
    """Move OWNER from one member of a tenant to another.

    Demote-then-promote inside one transaction, because the partial unique constraint
    forbids two live owners: promoting first would violate it. The old owner becomes an
    admin rather than a member — they were running the account a moment ago, and
    dropping them to the lowest role is a surprise nobody asked for.
    """
    current = mapping_for(current_owner)
    incoming = mapping_for(new_owner)

    if current is None or current.role != TenantUserMapping.Role.OWNER:
        raise PermissionDenied("Only the current owner can transfer ownership.")

    if incoming is None or incoming.tenant_id != current.tenant_id:
        raise PermissionDenied("The new owner must belong to the same organisation.")

    if incoming.pk == current.pk:
        return current

    # Locked so two concurrent transfers cannot both read "I am the owner" and race.
    # The constraint would reject the loser anyway; this turns a 500 into an ordered
    # outcome.
    locked = list(
        TenantUserMapping.objects.select_for_update()
        .filter(pk__in=[current.pk, incoming.pk])
        .order_by("pk")
    )
    by_pk = {row.pk: row for row in locked}
    current = by_pk[current.pk]
    incoming = by_pk[incoming.pk]

    if current.role != TenantUserMapping.Role.OWNER:
        raise PermissionDenied("Only the current owner can transfer ownership.")

    current.role = TenantUserMapping.Role.ADMIN
    current.save(update_fields=["role", "updated_at"])

    incoming.role = TenantUserMapping.Role.OWNER
    incoming.save(update_fields=["role", "updated_at"])

    logger.info(
        "tenant %s: ownership transferred from user %s to user %s",
        current.tenant_id,
        current.user_id,
        incoming.user_id,
    )
    return incoming


def release_seat(user, deleted_by=None):
    """Soft-delete a user's tenant mapping so their seat goes back to the plan.

    The `USER` allowance is counted live from live mappings
    (`onboard.services.entitlements`), and `TenantUserMapping.user` is a `OneToOneField`
    whose `CASCADE` never fires on a soft delete — a soft delete is a flag write, not a
    delete. So without this, removing a user leaves the mapping behind and the seat is
    consumed forever: a tenant that has churned through its allowance is full, with
    nobody to show for it, and the only symptom is a 402 they cannot explain.
    """
    mapping = mapping_for(user)
    if mapping is None:
        return None
    mapping.soft_delete(user=deleted_by)
    return mapping
