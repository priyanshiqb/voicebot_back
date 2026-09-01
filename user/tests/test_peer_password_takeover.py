"""
A password must not be settable through the user resource.

`CustomUserViewSet.get_queryset` deliberately widens the queryset to every user in the
requester's tenant, so colleagues can maintain each other's details — `test_isolation`
asserts that on purpose. The serializer used to accept `password` on update as well,
and the two together meant any tenant user could overwrite a colleague's credentials
and then log in as them.

These tests fix both halves in place: the takeover must stay closed, and the peer
editing it rode in on must keep working.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping

User = get_user_model()

OWN_PASSWORD = "SecurePassword123!"
ATTACKER_CHOICE = "AttackerChosen123!"


class PeerPasswordTakeoverTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-takeover")

        # The actor is an ADMIN on purpose. A member cannot reach a colleague's row at
        # all since roles landed, which would make these pass for the wrong reason —
        # the point is that the password block holds even for somebody who *is*
        # entitled to edit that row.
        self.attacker = self._user(
            "attacker@example.com", TenantUserMapping.Role.ADMIN
        )
        self.victim = self._user("victim@example.com", TenantUserMapping.Role.MEMBER)

        self.client.force_authenticate(user=self.attacker)

    def _user(self, email, role):
        user = User.objects.create_user(email=email, password=OWN_PASSWORD)
        user.is_active = True
        user.save()
        TenantUserMapping.objects.create(user=user, tenant=self.tenant, role=role)
        return user

    def _detail_url(self, user):
        return reverse("api-v1:customuser-detail", kwargs={"pk": user.pk})

    # -- the takeover ------------------------------------------------------

    def test_cannot_set_a_peers_password(self):
        response = self.client.patch(
            self._detail_url(self.victim), {"password": ATTACKER_CHOICE}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.victim.refresh_from_db()
        self.assertFalse(self.victim.check_password(ATTACKER_CHOICE))
        self.assertTrue(self.victim.check_password(OWN_PASSWORD))

    def test_cannot_set_own_password_this_way_either(self):
        """The endpoint that verifies the old password is the only one for this."""
        response = self.client.patch(
            self._detail_url(self.attacker), {"password": ATTACKER_CHOICE}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.attacker.refresh_from_db()
        self.assertTrue(self.attacker.check_password(OWN_PASSWORD))

    def test_rejection_is_explicit_not_silent(self):
        """A caller that thinks it changed a password and did not is worse off."""
        response = self.client.patch(
            self._detail_url(self.victim), {"password": ATTACKER_CHOICE}
        )

        self.assertIn("password", response.data.get("errors", response.data))

    def test_password_smuggled_alongside_a_legitimate_field_is_still_refused(self):
        response = self.client.patch(
            self._detail_url(self.victim),
            {"first_name": "Renamed", "password": ATTACKER_CHOICE},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.victim.refresh_from_db()
        self.assertTrue(self.victim.check_password(OWN_PASSWORD))
        # The whole request is rejected, so the innocent field does not land either.
        self.assertNotEqual(self.victim.first_name, "Renamed")

    def test_put_is_refused_as_well_as_patch(self):
        response = self.client.put(
            self._detail_url(self.victim),
            {"email": self.victim.email, "password": ATTACKER_CHOICE},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.victim.refresh_from_db()
        self.assertTrue(self.victim.check_password(OWN_PASSWORD))

    # -- what must keep working -------------------------------------------

    def test_peer_field_editing_still_works(self):
        """The documented behaviour the takeover rode in on. Do not lose it."""
        response = self.client.patch(
            self._detail_url(self.victim), {"first_name": "Renamed"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.victim.refresh_from_db()
        self.assertEqual(self.victim.first_name, "Renamed")

    def test_change_password_endpoint_still_works(self):
        """The legitimate path: it verifies the old password first."""
        response = self.client.post(
            reverse("api-v1:customuser-change-password"),
            {
                "old_password": OWN_PASSWORD,
                "new_password": ATTACKER_CHOICE,
                "confirm_password": ATTACKER_CHOICE,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.attacker.refresh_from_db()
        self.assertTrue(self.attacker.check_password(ATTACKER_CHOICE))

    def test_creating_a_user_with_a_password_still_works(self):
        """Create keeps it: today it is the only way to onboard somebody."""
        response = self.client.post(
            reverse("api-v1:customuser-list"),
            {"email": "newcomer@example.com", "password": "BrandNewPass123!"},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        newcomer = User.objects.get(email="newcomer@example.com")
        self.assertTrue(newcomer.check_password("BrandNewPass123!"))
