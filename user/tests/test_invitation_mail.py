"""
The invitation email's link.

The token exists only in this mail, so a mail without a usable link is an invitation
nobody can accept. Two things have to hold, and neither did: the URL needs a host — it
was built from `FRONTEND_BASE_URL`, which no environment defines, so it went out as a
bare path — and it has to appear in the HTML part, which is what a mail client shows.
"""

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from tenant.models.Tenant import Tenant
from tenant.models.TenantInvitation import TenantInvitation, hash_token
from tenant.models.TenantUserMapping import TenantUserMapping
from user.services import invitation_mail

User = get_user_model()

LIST_URL = "api-v1:invitation-list"
APP = "https://app.example.com"
INVITEE = "newcomer@example.com"


@override_settings(FRONTEND_BASE_URL="", FRONTEND_PROJECT_URL=APP)
class InvitationLinkTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-invite-mail")
        self.owner = User.objects.create_user(
            email="owner@example.com", password="SecurePassword123!"
        )
        self.owner.is_active = True
        self.owner.save()
        TenantUserMapping.objects.create(
            user=self.owner, tenant=self.tenant, role=TenantUserMapping.Role.OWNER
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        mail.outbox.clear()

    # ------------------------------------------------------------- the address

    def test_the_link_uses_the_frontend_every_environment_configures(self):
        """`FRONTEND_BASE_URL` is set in none of them; `FRONTEND_PROJECT_URL` is."""
        self.assertEqual(
            invitation_mail.accept_url("tok"), f"{APP}/accept-invitation?token=tok"
        )

    @override_settings(FRONTEND_BASE_URL="https://invites.example.com/")
    def test_an_explicit_base_wins_and_keeps_a_single_slash(self):
        self.assertEqual(
            invitation_mail.accept_url("tok"),
            "https://invites.example.com/accept-invitation?token=tok",
        )

    @override_settings(FRONTEND_BASE_URL="", FRONTEND_PROJECT_URL="")
    def test_with_nothing_configured_the_link_is_relative_and_logged(self):
        """Unusable either way — but it says so rather than guessing a host."""
        with self.assertLogs("user.services.invitation_mail", level="ERROR"):
            self.assertEqual(
                invitation_mail.accept_url("tok"), "/accept-invitation?token=tok"
            )

    # ------------------------------------------------------------- the mail

    def _invite(self, email=INVITEE):
        response = self.client.post(reverse(LIST_URL), {"email": email})
        # 202, not 201: the endpoint never says whether an invitation was created, so
        # that a tenant admin cannot enumerate which addresses already have accounts.
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(len(mail.outbox), 1, "no invitation mail was sent")
        return mail.outbox[0]

    def _html(self, message):
        self.assertTrue(message.alternatives, "the mail has no HTML part")
        return message.alternatives[0][0]

    def test_the_html_body_carries_a_clickable_link(self):
        """What the invitee actually sees; the plain-text part is rarely displayed."""
        html = self._html(self._invite())

        self.assertIn(f'href="{APP}/accept-invitation?token=', html)

    def test_the_html_body_also_prints_the_link_to_copy(self):
        """For clients that strip the button, and for people who paste links."""
        html = self._html(self._invite())

        self.assertIn(f"{APP}/accept-invitation?token=", html.split("Or copy this")[1])

    def test_the_plain_text_body_carries_the_link_too(self):
        self.assertIn(f"{APP}/accept-invitation?token=", self._invite().body)

    def test_the_link_opens_the_invitation_that_was_stored(self):
        token = self._invite().body.split("token=")[1].split()[0].strip()

        invitation = TenantInvitation.objects.get(email=INVITEE)
        self.assertEqual(invitation.token_hash, hash_token(token))
