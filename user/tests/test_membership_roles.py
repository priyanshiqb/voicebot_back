"""
Who at a tenant may act on whom.

`CustomUserViewSet.get_queryset` deliberately shows every colleague, so visibility is
not the control here — `TenantUserMapping.role` is. These tests are mostly about the
two things the role system exists to prevent: a member quietly deactivating or removing
a colleague, and an admin removing the owner and leaving the tenant with nobody who can
appoint anyone.

The seat cases live here too, because releasing the seat happens on the same delete path
the role check guards, and a leak there is invisible until a tenant cannot add anybody.
"""

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.services import entitlements
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from tenant.services import membership

User = get_user_model()

PASSWORD = "SecurePassword123!"


class RoleTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-roles")

        self.owner = self._user("owner@example.com", TenantUserMapping.Role.OWNER)
        self.admin = self._user("admin@example.com", TenantUserMapping.Role.ADMIN)
        self.member = self._user("member@example.com", TenantUserMapping.Role.MEMBER)
        self.other = self._user("other@example.com", TenantUserMapping.Role.MEMBER)

    def _user(self, email, role, tenant=None):
        user = User.objects.create_user(email=email, password=PASSWORD)
        user.is_active = True
        user.save()
        TenantUserMapping.objects.create(
            user=user, tenant=tenant or self.tenant, role=role
        )
        return user

    def _as(self, user):
        self.client.force_authenticate(user=user)
        return self.client

    def _detail(self, user):
        return reverse("api-v1:customuser-detail", kwargs={"pk": user.pk})


class MemberRestrictionTests(RoleTestCase):
    def test_member_cannot_edit_a_peer(self):
        response = self._as(self.member).patch(
            self._detail(self.other), {"first_name": "Renamed"}
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.other.refresh_from_db()
        self.assertNotEqual(self.other.first_name, "Renamed")

    def test_member_cannot_deactivate_a_peer(self):
        """The gap the password fix left open until roles existed."""
        response = self._as(self.member).patch(
            self._detail(self.other), {"is_active": False}
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.other.refresh_from_db()
        self.assertTrue(self.other.is_active)

    def test_member_cannot_delete_a_peer(self):
        response = self._as(self.member).delete(self._detail(self.other))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.other.refresh_from_db()
        self.assertFalse(self.other.is_deleted)

    def test_member_cannot_create_a_user(self):
        response = self._as(self.member).post(
            reverse("api-v1:customuser-list"),
            {"email": "newcomer@example.com", "password": PASSWORD},
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(User.objects.filter(email="newcomer@example.com").exists())

    def test_member_can_still_edit_themselves(self):
        response = self._as(self.member).patch(
            self._detail(self.member), {"first_name": "Self"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.member.refresh_from_db()
        self.assertEqual(self.member.first_name, "Self")

    def test_member_can_still_see_colleagues(self):
        """Visibility is not the control. Losing it would blind the roster."""
        response = self._as(self.member).get(reverse("api-v1:customuser-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        emails = [row["email"] for row in response.data["data"]]
        self.assertIn("owner@example.com", emails)


class AdminCapabilityTests(RoleTestCase):
    def test_admin_can_edit_a_member(self):
        response = self._as(self.admin).patch(
            self._detail(self.member), {"first_name": "Renamed"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.member.refresh_from_db()
        self.assertEqual(self.member.first_name, "Renamed")

    def test_admin_can_create_a_user(self):
        response = self._as(self.admin).post(
            reverse("api-v1:customuser-list"),
            {"email": "newcomer@example.com", "password": PASSWORD},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_a_created_user_arrives_as_a_member(self):
        """Least authority on arrival: nobody is created able to remove their creator."""
        self._as(self.admin).post(
            reverse("api-v1:customuser-list"),
            {"email": "newcomer@example.com", "password": PASSWORD},
        )

        newcomer = User.objects.get(email="newcomer@example.com")
        self.assertEqual(
            membership.role_of(newcomer), TenantUserMapping.Role.MEMBER
        )

    def test_admin_cannot_edit_the_owner(self):
        response = self._as(self.admin).patch(
            self._detail(self.owner), {"first_name": "Renamed"}
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_cannot_delete_the_owner(self):
        """Otherwise an admin can remove the billing relationship and nothing undoes it."""
        response = self._as(self.admin).delete(self._detail(self.owner))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_deleted)

    def test_admin_cannot_demote_the_owner(self):
        response = self._as(self.admin).post(
            reverse("api-v1:customuser-set-role", kwargs={"pk": self.owner.pk}),
            {"role": TenantUserMapping.Role.MEMBER},
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(membership.role_of(self.owner), TenantUserMapping.Role.OWNER)

    def test_owner_can_edit_an_admin(self):
        response = self._as(self.owner).patch(
            self._detail(self.admin), {"first_name": "Renamed"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class SetRoleTests(RoleTestCase):
    def _set_role(self, actor, target, role):
        return self._as(actor).post(
            reverse("api-v1:customuser-set-role", kwargs={"pk": target.pk}),
            {"role": role},
        )

    def test_admin_can_promote_a_member(self):
        response = self._set_role(
            self.admin, self.member, TenantUserMapping.Role.ADMIN
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(membership.role_of(self.member), TenantUserMapping.Role.ADMIN)

    def test_member_cannot_promote_themselves(self):
        """`can_act_on` permits self, so the manager check is what stops this."""
        response = self._set_role(
            self.member, self.member, TenantUserMapping.Role.ADMIN
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(membership.role_of(self.member), TenantUserMapping.Role.MEMBER)

    def test_owner_cannot_be_granted_through_set_role(self):
        """Ownership is transferred, never granted — there is only ever one."""
        response = self._set_role(
            self.owner, self.member, TenantUserMapping.Role.OWNER
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(membership.role_of(self.member), TenantUserMapping.Role.MEMBER)


class OwnershipTests(RoleTestCase):
    def _transfer(self, actor, target):
        return self._as(actor).post(
            reverse(
                "api-v1:customuser-transfer-ownership", kwargs={"pk": target.pk}
            )
        )

    def test_owner_can_transfer_ownership(self):
        response = self._transfer(self.owner, self.admin)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(membership.role_of(self.admin), TenantUserMapping.Role.OWNER)

    def test_the_previous_owner_becomes_an_admin(self):
        """Not a member: they were running the account a moment ago."""
        self._transfer(self.owner, self.admin)

        self.assertEqual(membership.role_of(self.owner), TenantUserMapping.Role.ADMIN)

    def test_transfer_leaves_exactly_one_owner(self):
        self._transfer(self.owner, self.admin)

        owners = TenantUserMapping.objects.filter(
            tenant=self.tenant, role=TenantUserMapping.Role.OWNER, is_deleted=False
        )
        self.assertEqual(owners.count(), 1)

    def test_an_admin_cannot_transfer_ownership(self):
        response = self._transfer(self.admin, self.member)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(membership.role_of(self.owner), TenantUserMapping.Role.OWNER)

    def test_a_second_owner_is_rejected_by_the_database(self):
        """The constraint, not the service, is what makes one-owner true under a race."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                mapping = membership.mapping_for(self.member)
                mapping.role = TenantUserMapping.Role.OWNER
                mapping.save(update_fields=["role"])

    def test_a_soft_deleted_owner_does_not_block_a_new_one(self):
        """The constraint is partial on is_deleted, so a tenant is never stuck."""
        membership.release_seat(self.owner)

        mapping = membership.mapping_for(self.member)
        mapping.role = TenantUserMapping.Role.OWNER
        mapping.save(update_fields=["role"])

        self.assertEqual(membership.role_of(self.member), TenantUserMapping.Role.OWNER)


class CrossTenantTests(RoleTestCase):
    def test_an_owner_cannot_reach_another_tenants_user(self):
        other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-roles")
        stranger = self._user(
            "stranger@example.com", TenantUserMapping.Role.MEMBER, tenant=other_tenant
        )

        response = self._as(self.owner).patch(
            self._detail(stranger), {"first_name": "Renamed"}
        )

        # 404 from the queryset, not 403 — a tenant should not learn the row exists.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class SeatAccountingTests(RoleTestCase):
    """The USER allowance counts live mappings, so removal must release one."""

    def _seats(self):
        return entitlements.current_usage(self.tenant.id, EntitlementResource.USER)

    def test_deleting_a_user_frees_the_seat(self):
        before = self._seats()

        response = self._as(self.owner).delete(self._detail(self.member))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._seats(), before - 1)

    def test_the_mapping_is_soft_deleted_not_just_the_user(self):
        """The FK cascade never fires on a soft delete, so this must be explicit."""
        self._as(self.owner).delete(self._detail(self.member))

        self.assertFalse(
            TenantUserMapping.objects.filter(
                user=self.member, is_deleted=False
            ).exists()
        )

    def test_bulk_delete_frees_every_seat(self):
        before = self._seats()

        response = self._as(self.owner).post(
            reverse("api-v1:customuser-bulk-delete"),
            {"ids": [str(self.member.pk), str(self.other.pk)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._seats(), before - 2)

    def test_bulk_delete_refuses_a_batch_containing_the_owner(self):
        """All-or-nothing, so the response cannot identify the protected id."""
        before = self._seats()

        response = self._as(self.admin).post(
            reverse("api-v1:customuser-bulk-delete"),
            {"ids": [str(self.member.pk), str(self.owner.pk)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._seats(), before)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_deleted)

    def test_a_member_cannot_bulk_delete(self):
        before = self._seats()

        response = self._as(self.member).post(
            reverse("api-v1:customuser-bulk-delete"),
            {"ids": [str(self.other.pk)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._seats(), before)
