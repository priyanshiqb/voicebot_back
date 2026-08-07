"""
Tests for per-tenant settings, and specifically for the booking-email assignment.

The assignment decides whether a tenant's customers receive booking mail at all —
there is no platform fallback — so the rules around it are stricter than ordinary
CRUD:

* Only **this tenant's** configurations can be assigned.
* Only a **verified, connected** one can be assigned, because assigning a broken
  sender looks identical to assigning nothing.
* The assigned configuration cannot be disconnected or deleted out from under the
  assignment.
"""

from cryptography.fernet import Fernet
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from tenant.models.Tenant import Tenant
from tenant.models.TenantEmailConfiguration import TenantEmailConfiguration
from tenant.models.TenantSettings import TenantSettings
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

SETTINGS_URL = "/api/v1/tenant/settings/"
CONFIG_URL = "/api/v1/tenant/email-configurations/"

TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()


@override_settings(SCHEDULING_TOKEN_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY)
class TenantSettingsTestCase(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-settings")
        self.other_tenant = Tenant.objects.create(
            name="Globex", subdomain="globex-settings"
        )

        self.user = User.objects.create_user(
            username="set-alex", email="alex@acme.test", password="password"
        )
        self.other_user = User.objects.create_user(
            username="set-bob", email="bob@globex.test", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.other_user, tenant=self.other_tenant)

        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(user=self.other_user)

    def make_config(self, tenant=None, verified=True, active=True, name="Primary"):
        config = TenantEmailConfiguration.objects.create(
            tenant=tenant or self.tenant,
            name=name,
            host="smtp.test.invalid",
            port=587,
            from_email="bookings@acme.test",
            is_active=active,
            status=(
                TenantEmailConfiguration.Status.VERIFIED
                if verified
                else TenantEmailConfiguration.Status.UNVERIFIED
            ),
        )
        config.make_default()
        return config


class SettingsReadTests(TenantSettingsTestCase):
    def test_settings_are_created_on_first_read(self):
        """Reading must never fail because nobody has written yet — the same code
        path decides whether a booking confirmation can be sent."""
        self.assertEqual(TenantSettings.objects.count(), 0)

        response = self.client_alex.get(SETTINGS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(TenantSettings.objects.count(), 1)
        self.assertIsNone(response.data["data"]["booking_email_configuration"])

    def test_an_unconfigured_tenant_is_told_booking_mail_is_not_sending(self):
        response = self.client_alex.get(SETTINGS_URL)
        state = response.data["data"]["booking_email"]

        self.assertFalse(state["will_send"])
        self.assertIn("not being sent", state["reason"])
        self.assertIsNone(state["configuration_id"])

    def test_a_configured_tenant_is_told_what_will_send(self):
        config = self.make_config()
        self.client_alex.patch(
            SETTINGS_URL, {"booking_email_configuration": str(config.id)}, format="json"
        )

        state = self.client_alex.get(SETTINGS_URL).data["data"]["booking_email"]
        self.assertTrue(state["will_send"])
        self.assertIsNone(state["reason"])
        self.assertEqual(state["from_email"], config.from_email)

    def test_a_disconnected_assignment_explains_itself(self):
        """The pointer stays populated while what it points at stops working. A
        tenant staring at a filled-in field needs to be told why nothing arrives."""
        config = self.make_config()
        self.client_alex.patch(
            SETTINGS_URL, {"booking_email_configuration": str(config.id)}, format="json"
        )
        TenantEmailConfiguration.objects.filter(pk=config.pk).update(is_active=False)

        state = self.client_alex.get(SETTINGS_URL).data["data"]["booking_email"]
        self.assertFalse(state["will_send"])
        self.assertIn("disconnected", state["reason"])
        self.assertEqual(state["configuration_id"], str(config.id))

    def test_an_unverified_assignment_explains_itself(self):
        config = self.make_config()
        self.client_alex.patch(
            SETTINGS_URL, {"booking_email_configuration": str(config.id)}, format="json"
        )
        TenantEmailConfiguration.objects.filter(pk=config.pk).update(
            status=TenantEmailConfiguration.Status.UNVERIFIED
        )

        state = self.client_alex.get(SETTINGS_URL).data["data"]["booking_email"]
        self.assertFalse(state["will_send"])
        self.assertIn("test send", state["reason"])

    def test_authentication_is_required(self):
        self.assertEqual(
            APIClient().get(SETTINGS_URL).status_code, status.HTTP_401_UNAUTHORIZED
        )


class SettingsWriteTests(TenantSettingsTestCase):
    def test_assigning_a_verified_configuration_works(self):
        config = self.make_config()
        response = self.client_alex.patch(
            SETTINGS_URL, {"booking_email_configuration": str(config.id)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(TenantSettings.booking_sender(self.tenant.id).pk, config.pk)

    def test_an_unverified_configuration_cannot_be_assigned(self):
        """Assigning one that cannot send leaves booking mail silently off, which
        looks identical to having configured nothing."""
        config = self.make_config(verified=False)
        response = self.client_alex.patch(
            SETTINGS_URL, {"booking_email_configuration": str(config.id)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("test send", str(response.data))

    def test_a_disconnected_configuration_cannot_be_assigned(self):
        config = self.make_config(active=False)
        response = self.client_alex.patch(
            SETTINGS_URL, {"booking_email_configuration": str(config.id)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("disconnected", str(response.data))

    def test_another_tenants_configuration_cannot_be_assigned(self):
        """The pointer arrives as an id. Unscoped, a tenant could route their own
        customer mail through a stranger's credentials by naming a UUID."""
        foreign = self.make_config(tenant=self.other_tenant, name="Theirs")
        response = self.client_alex.patch(
            SETTINGS_URL,
            {"booking_email_configuration": str(foreign.id)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(TenantSettings.booking_sender(self.tenant.id))

    def test_the_assignment_can_be_cleared(self):
        """The escape hatch. A tenant with a leaked credential must be able to stop
        sending immediately, accepting that booking mail stops with it."""
        config = self.make_config()
        self.client_alex.patch(
            SETTINGS_URL, {"booking_email_configuration": str(config.id)}, format="json"
        )

        response = self.client_alex.patch(
            SETTINGS_URL, {"booking_email_configuration": None}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(TenantSettings.booking_sender(self.tenant.id))
        self.assertFalse(response.data["data"]["booking_email"]["will_send"])

    def test_each_tenant_has_its_own_settings(self):
        mine = self.make_config()
        self.client_alex.patch(
            SETTINGS_URL, {"booking_email_configuration": str(mine.id)}, format="json"
        )

        theirs = self.client_bob.get(SETTINGS_URL)
        self.assertIsNone(theirs.data["data"]["booking_email_configuration"])
        self.assertEqual(TenantSettings.objects.count(), 2)


class AssignedSenderNeedsConfirmationTests(TenantSettingsTestCase):
    """Taking the assigned sender out of service asks first, then goes through with
    `force=true` and clears the assignment.

    Two failure modes, and the gate exists because both are real. Refusing outright
    never prevented the outcome — a tenant who wants a configuration gone edits
    settings and comes back — while slowing down the leaked-credential case. But
    allowing it silently is worse: switching off one connection among several stops
    every booking confirmation, and nothing in the request hints at that.

    Once forced, the assignment must not survive. A pointer left aimed at a
    disconnected or deleted configuration reports a sender that cannot send, which
    is indistinguishable from a misconfiguration when someone comes to work out why
    nothing arrived."""

    def setUp(self):
        super().setUp()
        self.config = self.make_config()
        self.client_alex.patch(
            SETTINGS_URL,
            {"booking_email_configuration": str(self.config.id)},
            format="json",
        )

    def _assignment(self):
        return TenantSettings.objects.get(
            tenant=self.tenant
        ).booking_email_configuration_id

    # --- the confirmation gate ------------------------------------------------

    def test_disconnecting_it_asks_for_confirmation_first(self):
        response = self.client_alex.post(f"{CONFIG_URL}{self.config.id}/disconnect/")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(response.data["errors"]["code"], "BOOKING_SENDER_IN_USE")
        self.assertTrue(response.data["errors"]["requires_force"])

        message = response.data["message"]
        # Reads as a confirm dialog: a question, then the consequence.
        self.assertTrue(message.startswith("Are you sure"), message)
        self.assertIn("disconnect this email integration", message)
        self.assertIn("demo booking emails", message)

        # It is rendered verbatim in a popup, so nothing a tenant cannot act on
        # belongs in it. How to proceed is machine detail — `requires_force` above.
        for leak in ("force", "/api/v1/", "booking_email_configuration", "409"):
            self.assertNotIn(leak, message, f"{leak!r} leaked into popup copy")

        # Nothing moved. A refusal that half-applied would be worse than no gate.
        self.config.refresh_from_db()
        self.assertTrue(self.config.is_active)
        self.assertEqual(self._assignment(), self.config.id)

    def test_deleting_it_asks_for_confirmation_first(self):
        response = self.client_alex.delete(f"{CONFIG_URL}{self.config.id}/")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        # The verb matches the action — "disconnect" in a delete dialog would be
        # the kind of copy bug nobody notices until a tenant does.
        self.assertIn("delete this email integration", response.data["message"])
        self.assertIn("demo booking emails", response.data["message"])

        self.assertTrue(
            TenantEmailConfiguration.objects.filter(pk=self.config.pk).exists()
        )
        self.assertEqual(self._assignment(), self.config.id)

    def test_bulk_deleting_it_asks_for_confirmation_first(self):
        spare = self.make_config(name="Spare")
        response = self.client_alex.post(
            f"{CONFIG_URL}bulk_delete/",
            {"ids": [str(spare.id), str(self.config.id)]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        # Plural prompt, and a note that agrees with it: only one configuration per
        # tenant can be the booking sender, so "It is currently used" after a
        # plural prompt would read as though all of them are.
        self.assertIn("selected email integrations", response.data["message"])
        self.assertIn("One of them is currently used", response.data["message"])
        # All-or-nothing: the spare is not deleted either.
        self.assertEqual(
            TenantEmailConfiguration.objects.filter(tenant=self.tenant).count(), 2
        )

    def test_an_unassigned_configuration_needs_no_confirmation(self):
        """The gate is about consequence, not ceremony — spares go straight
        through."""
        spare = self.make_config(name="Spare")

        response = self.client_alex.post(f"{CONFIG_URL}{spare.id}/disconnect/")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._assignment(), self.config.id)

    # --- forcing through ------------------------------------------------------

    def test_forcing_disconnect_clears_the_assignment(self):
        response = self.client_alex.post(
            f"{CONFIG_URL}{self.config.id}/disconnect/",
            {"force": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.config.refresh_from_db()
        self.assertFalse(self.config.is_active)
        self.assertIsNone(self._assignment())

    def test_forcing_delete_clears_the_assignment(self):
        """DELETE carries no body in most clients, so the query string has to work."""
        response = self.client_alex.delete(f"{CONFIG_URL}{self.config.id}/?force=true")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.assertFalse(
            TenantEmailConfiguration.objects.filter(pk=self.config.pk).exists()
        )
        self.assertIsNone(self._assignment())

    def test_forcing_bulk_delete_clears_the_assignment(self):
        """Bulk delete does not go through `perform_destroy`, so the release has to
        live on the model's soft_delete or this path leaves a dangling pointer."""
        spare = self.make_config(name="Spare")
        response = self.client_alex.post(
            f"{CONFIG_URL}bulk_delete/",
            {"ids": [str(spare.id), str(self.config.id)], "force": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.assertEqual(
            TenantEmailConfiguration.objects.filter(tenant=self.tenant).count(), 0
        )
        self.assertIsNone(self._assignment())

    def test_force_is_accepted_in_the_spellings_a_client_will_send(self):
        """`force=1` from a query string and `"true"` from JSON are the same
        intent; silently ignoring either looks like the flag does not work."""
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                config = self.make_config(name=f"Assigned {value}")
                self.client_alex.patch(
                    SETTINGS_URL,
                    {"booking_email_configuration": str(config.id)},
                    format="json",
                )

                response = self.client_alex.delete(
                    f"{CONFIG_URL}{config.id}/?force={value}"
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_falsy_force_still_asks(self):
        response = self.client_alex.post(
            f"{CONFIG_URL}{self.config.id}/disconnect/",
            {"force": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_the_settings_endpoint_reports_that_nothing_will_send(self):
        """The state a tenant is left in has to be readable, not just internally
        consistent."""
        self.client_alex.post(
            f"{CONFIG_URL}{self.config.id}/disconnect/",
            {"force": True},
            format="json",
        )

        booking_email = self.client_alex.get(SETTINGS_URL).data["data"]["booking_email"]
        self.assertFalse(booking_email["will_send"])
        self.assertIsNone(booking_email["configuration_id"])
        self.assertIn("No email configuration is assigned", booking_email["reason"])

    def test_clearing_the_assignment_releases_it(self):
        """The documented way out: say what should carry booking mail instead, or
        say that nothing should."""
        self.client_alex.patch(
            SETTINGS_URL, {"booking_email_configuration": None}, format="json"
        )

        response = self.client_alex.post(f"{CONFIG_URL}{self.config.id}/disconnect/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.config.refresh_from_db()
        self.assertFalse(self.config.is_active)

    def test_reassigning_releases_the_previous_sender(self):
        replacement = self.make_config(name="Replacement")
        self.client_alex.patch(
            SETTINGS_URL,
            {"booking_email_configuration": str(replacement.id)},
            format="json",
        )

        response = self.client_alex.post(f"{CONFIG_URL}{self.config.id}/disconnect/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_deleting_through_a_path_that_skips_the_guard_clears_the_pointer(self):
        """`on_delete=SET_NULL` never fires for soft deletes, so a stale id would
        otherwise survive and make the settings row claim to be configured."""
        self.config.release_from_settings()
        self.config.soft_delete()

        settings_row = TenantSettings.objects.get(tenant=self.tenant)
        self.assertIsNone(settings_row.booking_email_configuration_id)
