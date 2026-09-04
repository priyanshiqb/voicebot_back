from django.db import models
from django.conf import settings
from utils.models.base_model import BaseModel
from tenant.models.Tenant import Tenant


class TenantUserMapping(BaseModel):
    """
    Tenant User Mapping Model.
    Enforces a strict 1-to-1 association where each user account is mapped to exactly one tenant organization.
    Inherits from global BaseModel for user auditing and soft-deletion capability.

    This is also where a person's authority *inside* their tenant lives. See ``role``:
    ``CustomUser.UserType`` says permissions come from the tenant mapping rather than
    from the user, and this field is what finally makes that true.
    """

    class Role(models.TextChoices):
        """What somebody may do to the other people at their tenant.

        Three, not a permission matrix, for the reason ``CustomUser.PlatformRole``
        already gives about the console: a matrix nobody can hold in their head is one
        that gets worked around.

        ``OWNER`` exists to stop a tenant locking itself out. Exactly one per tenant,
        enforced by a database constraint rather than by convention, and nobody else
        can remove or demote them -- otherwise two admins can remove each other, or
        remove the person who holds the billing relationship, and nobody is left who
        can add anyone back.

        ``ADMIN`` manages people but cannot touch the owner.

        ``MEMBER`` maintains only themselves. It is the default because a new account
        should arrive with the least authority, not the most.
        """

        OWNER = "OWNER", "Owner"
        ADMIN = "ADMIN", "Admin"
        MEMBER = "MEMBER", "Member"

    #: Roles allowed to add, remove, and edit other people.
    MANAGER_ROLES = [Role.OWNER, Role.ADMIN]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_mapping",
        help_text="Strict 1:1 user profile assignment constraint."
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="user_mappings",
        help_text="Mapped organization/tenant ownership."
    )

    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.MEMBER,
        db_index=True,
        help_text="What this person may do to other people at their tenant.",
    )

    class Meta(BaseModel.Meta):
        verbose_name = "Tenant User Mapping"
        verbose_name_plural = "Tenant User Mappings"
        constraints = [
            # "Exactly one owner" as a database fact rather than a service convention.
            # A race between two concurrent ownership transfers cannot be closed in
            # Python without locking the whole tenant, and a tenant with two owners is
            # a tenant where either can remove the other.
            #
            # Partial on is_deleted: a soft-deleted mapping keeps its role, and without
            # the condition a tenant whose owner ever left could never appoint another.
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(role="OWNER", is_deleted=False),
                name="uq_tenant_single_owner",
            ),
        ]

    def __str__(self):
        return f"{self.user.username} -> {self.tenant.name}"


#: Roles that can be granted directly, for serializers that offer a choice.
#: OWNER is absent deliberately: ownership is *transferred* between existing members,
#: never granted, and the single-owner constraint would reject a second one anyway.
#: Defined once so the invite and set-role payloads cannot drift, and so the schema
#: has one name for this set instead of two generated ones.
ASSIGNABLE_ROLE_CHOICES = [
    (TenantUserMapping.Role.ADMIN, TenantUserMapping.Role.ADMIN.label),
    (TenantUserMapping.Role.MEMBER, TenantUserMapping.Role.MEMBER.label),
]
