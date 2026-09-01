"""
Tenant seat invitations.

Three invariants carry the weight, and most of these exercise one of them:

* **The seat is spent at invite time and returned on revoke.** The USER allowance counts
  live mappings, so anything else lets a tenant issue more invitations than it has
  seats and find out only when people accept.
* **An un-accepted invitation is not a way in.** The account exists but is inactive with
  an unusable password, and login gates on `is_active`.
* **Nothing here reveals which addresses are registered.** Inviting an existing account
  and inviting a stranger return the same response; only the mail differs.

Mail is mocked throughout — the point of these tests is the state machine, and a real
`send_email_task.delay` would need a broker.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.services import entitlements
from tenant.models.Tenant import Tenant
from tenant.models.TenantInvitation import TenantInvitation, hash_token
from tenant.models.TenantUserMapping import TenantUserMapping
from tenant.services import invitations as invitation_service
from tenant.services import membership

User = get_user_model()

PASSWORD = "SecurePassword123!"
NEW_PASSWORD = "InviteeChosen456!"

LIST_URL = "api-v1:invitation-list"
ACCEPT_URL = "api-v1:invitation-accept"


class InvitationTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-invite")

        self.owner = self._user("owner@example.com", TenantUserMapping.Role.OWNER)
        self.admin = self._user("admin@example.com", TenantUserMapping.Role.ADMIN)
        self.member = self._user("member@example.com", TenantUserMapping.Role.MEMBER)

        # Patched at the call site rather than at the task, so the assertion is "we
        # asked for the right mail", not "celery works".
        patcher = mock.patch(
            "user.views.invitation.invitation_mail.send_invitation", return_value=True
        )
        self.send_invitation = patcher.start()
        self.addCleanup(patcher.stop)

        patcher2 = mock.patch(
            "user.views.invitation.invitation_mail.send_already_registered",
            return_value=True,
        )
        self.send_already_registered = patcher2.start()
        self.addCleanup(patcher2.stop)

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

    def _invite(self, actor, email="newcomer@example.com", role=None):
        payload = {"email": email}
        if role:
            payload["role"] = role
        return self._as(actor).post(reverse(LIST_URL), payload)

    def _token_from_mail(self):
        """The raw token as handed to the mailer — it exists nowhere else."""
        return self.send_invitation.call_args.kwargs["raw_token"]

    def _seats(self):
        return entitlements.current_usage(self.tenant.id, EntitlementResource.USER)


class InvitePermissionTests(InvitationTestCase):
    def test_owner_can_invite(self):
        response = self._invite(self.owner)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_admin_can_invite(self):
        response = self._invite(self.admin)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_member_cannot_invite(self):
        response = self._invite(self.member)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(TenantInvitation.objects.exists())

    def test_anonymous_cannot_invite(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(reverse(LIST_URL), {"email": "x@example.com"})

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_owner_role_cannot_be_invited(self):
        response = self._invite(self.owner, role=TenantUserMapping.Role.OWNER)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_member_cannot_list_invitations(self):
        response = self._as(self.member).get(reverse(LIST_URL))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class InviteCreationTests(InvitationTestCase):
    def test_creates_an_inactive_account_with_no_usable_password(self):
        self._invite(self.owner)

        invited = User.objects.get(email="newcomer@example.com")
        self.assertFalse(invited.is_active)
        self.assertFalse(invited.has_usable_password())

    def test_the_invitee_cannot_log_in_before_accepting(self):
        """`is_active` is the gate that makes a pending invitation inert."""
        self._invite(self.owner)

        response = self.client.post(
            reverse("api-v1:login"),
            {"email": "newcomer@example.com", "password": PASSWORD},
        )
        self.assertNotEqual(response.status_code, status.HTTP_200_OK)

    def test_the_mapping_carries_the_invited_role(self):
        self._invite(self.owner, role=TenantUserMapping.Role.ADMIN)

        invited = User.objects.get(email="newcomer@example.com")
        self.assertEqual(membership.role_of(invited), TenantUserMapping.Role.ADMIN)

    def test_default_role_is_member(self):
        self._invite(self.owner)

        invited = User.objects.get(email="newcomer@example.com")
        self.assertEqual(membership.role_of(invited), TenantUserMapping.Role.MEMBER)

    def test_the_raw_token_is_never_stored(self):
        self._invite(self.owner)
        raw = self._token_from_mail()

        invitation = TenantInvitation.objects.get(email="newcomer@example.com")
        self.assertNotEqual(invitation.token_hash, raw)
        self.assertEqual(invitation.token_hash, hash_token(raw))

    def test_reinviting_replaces_rather_than_accumulates(self):
        """Several live tokens for one seat would be several ways in for one person."""
        self._invite(self.owner)
        first_token = self._token_from_mail()

        self._invite(self.owner)

        live = TenantInvitation.objects.filter(
            email="newcomer@example.com", is_deleted=False
        )
        self.assertEqual(live.count(), 1)
        self.assertIsNone(invitation_service.find_by_token(first_token))


class NonDisclosureTests(InvitationTestCase):
    """Inviting a registered address must be indistinguishable from inviting a stranger."""

    def test_inviting_an_existing_account_returns_the_same_status(self):
        fresh = self._invite(self.owner, email="stranger@example.com")
        existing = self._invite(self.owner, email="member@example.com")

        self.assertEqual(existing.status_code, fresh.status_code)
        self.assertEqual(existing.status_code, status.HTTP_202_ACCEPTED)

    def test_inviting_an_existing_account_returns_the_same_body(self):
        fresh = self._invite(self.owner, email="stranger@example.com")
        existing = self._invite(self.owner, email="member@example.com")

        self.assertEqual(existing.data, fresh.data)

    def test_no_invitation_or_account_is_created_for_an_existing_address(self):
        self._invite(self.owner, email="member@example.com")

        self.assertFalse(
            TenantInvitation.objects.filter(email="member@example.com").exists()
        )
        self.assertEqual(User.objects.filter(email="member@example.com").count(), 1)

    def test_the_existing_account_holder_is_told_instead(self):
        """The person learns they were invited; the inviter learns nothing."""
        self._invite(self.owner, email="member@example.com")

        self.send_already_registered.assert_called_once()
        self.send_invitation.assert_not_called()

    def test_an_address_at_another_tenant_is_also_not_disclosed(self):
        other = Tenant.objects.create(name="Globex", subdomain="globex-invite")
        self._user("rival@example.com", TenantUserMapping.Role.OWNER, tenant=other)

        response = self._invite(self.owner, email="rival@example.com")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.send_already_registered.assert_called_once()


class AcceptTests(InvitationTestCase):
    def _accept(self, token, password=NEW_PASSWORD):
        self.client.force_authenticate(user=None)
        return self.client.post(
            reverse(ACCEPT_URL),
            {"token": token, "password": password, "confirm_password": password},
        )

    def test_accepting_activates_the_account(self):
        self._invite(self.owner)
        response = self._accept(self._token_from_mail())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invited = User.objects.get(email="newcomer@example.com")
        self.assertTrue(invited.is_active)
        self.assertTrue(invited.check_password(NEW_PASSWORD))

    def test_accepting_is_unauthenticated(self):
        """The invitee has no account to authenticate with — that is the point."""
        self._invite(self.owner)
        self.client.force_authenticate(user=None)

        response = self._accept(self._token_from_mail())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_invitee_can_log_in_afterwards(self):
        self._invite(self.owner)
        self._accept(self._token_from_mail())

        response = self.client.post(
            reverse("api-v1:login"),
            {"email": "newcomer@example.com", "password": NEW_PASSWORD},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_token_cannot_be_used_twice(self):
        self._invite(self.owner)
        token = self._token_from_mail()
        self._accept(token)

        response = self._accept(token, password="SecondAttempt789!")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_unknown_token_is_refused(self):
        response = self._accept("not-a-real-token")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_expired_token_is_refused(self):
        self._invite(self.owner)
        token = self._token_from_mail()
        TenantInvitation.objects.filter(email="newcomer@example.com").update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        response = self._accept(token)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.get(email="newcomer@example.com").is_active)

    def test_a_revoked_token_is_refused(self):
        self._invite(self.owner)
        token = self._token_from_mail()
        invitation_service.revoke(
            TenantInvitation.objects.get(email="newcomer@example.com")
        )

        response = self._accept(token)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_every_failure_gives_the_same_message(self):
        """Otherwise the endpoint is an oracle for which tokens exist."""
        unknown = self._accept("not-a-real-token")

        self._invite(self.owner)
        token = self._token_from_mail()
        TenantInvitation.objects.filter(email="newcomer@example.com").update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        expired = self._accept(token)

        # `request_id` is a per-request correlation id and differs by design; it says
        # nothing about the token. Everything that describes the failure must match.
        self.assertEqual(unknown.data["message"], expired.data["message"])
        self.assertEqual(unknown.data["errors"], expired.data["errors"])
        self.assertEqual(unknown.status_code, expired.status_code)

    def test_mismatched_confirmation_is_refused(self):
        self._invite(self.owner)
        token = self._token_from_mail()

        self.client.force_authenticate(user=None)
        response = self.client.post(
            reverse(ACCEPT_URL),
            {
                "token": token,
                "password": NEW_PASSWORD,
                "confirm_password": "SomethingElse123!",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SeatAccountingTests(InvitationTestCase):
    def test_inviting_spends_a_seat_immediately(self):
        before = self._seats()
        self._invite(self.owner)

        self.assertEqual(self._seats(), before + 1)

    def test_accepting_does_not_spend_a_second_seat(self):
        self._invite(self.owner)
        after_invite = self._seats()

        self._accept_helper()

        self.assertEqual(self._seats(), after_invite)

    def _accept_helper(self):
        self.client.force_authenticate(user=None)
        token = self._token_from_mail()
        self.client.post(
            reverse(ACCEPT_URL),
            {
                "token": token,
                "password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )

    def test_revoking_returns_the_seat(self):
        before = self._seats()
        self._invite(self.owner)
        invitation = TenantInvitation.objects.get(email="newcomer@example.com")

        response = self._as(self.owner).post(
            reverse("api-v1:invitation-revoke", kwargs={"pk": invitation.pk})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._seats(), before)

    def test_revoking_frees_the_address_to_be_invited_again(self):
        """A soft delete would not: `email` is unique at the database level."""
        self._invite(self.owner)
        invitation_service.revoke(
            TenantInvitation.objects.get(email="newcomer@example.com")
        )

        response = self._invite(self.owner)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_revoking_an_accepted_invitation_is_refused(self):
        self._invite(self.owner)
        token = self._token_from_mail()
        self.client.force_authenticate(user=None)
        self.client.post(
            reverse(ACCEPT_URL),
            {"token": token, "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD},
        )
        invitation = TenantInvitation.objects.get(email="newcomer@example.com")

        response = self._as(self.owner).post(
            reverse("api-v1:invitation-revoke", kwargs={"pk": invitation.pk})
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sweep_expired_reclaims_seats(self):
        before = self._seats()
        self._invite(self.owner)
        TenantInvitation.objects.filter(email="newcomer@example.com").update(
            expires_at=timezone.now() - timedelta(days=1)
        )

        released = invitation_service.sweep_expired(tenant_id=self.tenant.id)

        self.assertEqual(released, 1)
        self.assertEqual(self._seats(), before)

    def test_sweep_leaves_live_invitations_alone(self):
        self._invite(self.owner)

        self.assertEqual(invitation_service.sweep_expired(tenant_id=self.tenant.id), 0)


class TenantScopingTests(InvitationTestCase):
    def test_invitations_from_another_tenant_are_not_listed(self):
        other = Tenant.objects.create(name="Globex", subdomain="globex-scope")
        other_owner = self._user(
            "globex-owner@example.com", TenantUserMapping.Role.OWNER, tenant=other
        )
        self._invite(other_owner, email="globex-hire@example.com")

        response = self._as(self.owner).get(reverse(LIST_URL))

        emails = [row["email"] for row in response.data["data"]]
        self.assertNotIn("globex-hire@example.com", emails)

    def test_another_tenants_invitation_cannot_be_revoked(self):
        other = Tenant.objects.create(name="Globex", subdomain="globex-revoke")
        other_owner = self._user(
            "globex-owner2@example.com", TenantUserMapping.Role.OWNER, tenant=other
        )
        self._invite(other_owner, email="globex-hire2@example.com")
        foreign = TenantInvitation.objects.get(email="globex-hire2@example.com")

        response = self._as(self.owner).post(
            reverse("api-v1:invitation-revoke", kwargs={"pk": foreign.pk})
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class TokenSecrecyTests(InvitationTestCase):
    def test_the_token_hash_is_never_serialised(self):
        """A list of pending invitations must not double as a list of live ways in."""
        self._invite(self.owner)

        response = self._as(self.owner).get(reverse(LIST_URL))

        self.assertNotIn("token_hash", response.data["data"][0])
        self.assertNotIn("token", response.data["data"][0])
