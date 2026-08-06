"""
Tests for tenant-owned SMTP configuration.

Three things here are worth more care than ordinary CRUD, and most of these tests
exist for one of them:

* **Credentials.** The password is encrypted at rest and must never leave the API
  in any form. A PATCH that omits it must not wipe it.
* **The single-default invariant.** A tenant may hold several configurations but
  exactly one default. Losing the default is silent — mail keeps flowing on the
  platform sender, from the wrong address, until somebody notices.
* **SSRF.** The tenant supplies the hostname our workers dial.

Host validation performs a real DNS lookup, so tests that are not *about* host
validation stub it out. The ones that are use literal IPs and `localhost`, which
resolve without touching the network.
"""

import smtplib
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from tenant.models.Tenant import Tenant
from tenant.models.TenantEmailConfiguration import TenantEmailConfiguration
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User
from utils.mail import connection as mail_connection
from utils.mail import providers

URL = "/api/v1/tenant/email-configurations/"

# A real Fernet key so encryption is exercised rather than stubbed, without
# depending on whatever the environment happens to have configured.
TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()


@override_settings(SCHEDULING_TOKEN_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY)
class EmailConfigurationTestCase(APITestCase):
    """Shared fixtures: two tenants, one user each."""

    def setUp(self):
        cache.clear()
        self.tenant = Tenant.objects.create(name="Acme Corp", subdomain="acme-mail")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-mail")

        self.user = User.objects.create_user(
            username="mail-alex", email="alex@acme.test", password="password"
        )
        self.other_user = User.objects.create_user(
            username="mail-bob", email="bob@globex.test", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.other_user, tenant=self.other_tenant)

        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(user=self.other_user)

        # Host validation is exercised by its own test class. Everywhere else it
        # would mean a live DNS lookup per request.
        patcher = patch("utils.mail.validators.validate_host", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def payload(self, **overrides):
        data = {
            "name": "Primary",
            "provider": "SENDGRID",
            "username": "apikey",
            "password": "SG.super-secret",
            "from_email": "bookings@acme.test",
            "from_name": "Acme Bookings",
        }
        data.update(overrides)
        return data

    def create_config(self, client=None, **overrides):
        client = client or self.client_alex
        response = client.post(URL, self.payload(**overrides), format="json")
        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED, response.data
        )
        return response.data["data"]


class CredentialHandlingTests(EmailConfigurationTestCase):
    def test_password_is_encrypted_at_rest_and_round_trips(self):
        self.create_config()
        config = TenantEmailConfiguration.objects.get()

        self.assertNotEqual(config.password, "SG.super-secret")
        self.assertNotIn("SG.super-secret", config.password)
        self.assertEqual(config.get_password(), "SG.super-secret")
        self.assertTrue(config.has_password())

    def test_password_never_appears_in_any_response(self):
        created = self.create_config()
        detail = self.client_alex.get(f"{URL}{created['id']}/")
        listing = self.client_alex.get(URL)

        for blob in (str(created), str(detail.data), str(listing.data)):
            self.assertNotIn("SG.super-secret", blob)
            self.assertNotIn("password':", blob.replace("has_password", ""))

        self.assertTrue(detail.data["data"]["has_password"])

    def test_patch_without_password_preserves_the_stored_one(self):
        """The easiest bug to ship here: a partial update reaching set_password(None)
        silently blanks a working configuration."""
        created = self.create_config()

        response = self.client_alex.patch(
            f"{URL}{created['id']}/", {"from_name": "Acme Support"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        config = TenantEmailConfiguration.objects.get()
        self.assertEqual(config.get_password(), "SG.super-secret")
        self.assertEqual(config.from_name, "Acme Support")

    def test_patch_with_a_new_password_replaces_it(self):
        created = self.create_config()
        self.client_alex.patch(
            f"{URL}{created['id']}/", {"password": "SG.rotated"}, format="json"
        )
        self.assertEqual(
            TenantEmailConfiguration.objects.get().get_password(), "SG.rotated"
        )


class VerificationStatusTests(EmailConfigurationTestCase):
    def test_editing_the_connection_resets_a_verified_configuration(self):
        created = self.create_config()
        config = TenantEmailConfiguration.objects.get()
        config.status = TenantEmailConfiguration.Status.VERIFIED
        config.save(update_fields=["status"])

        self.client_alex.patch(
            f"{URL}{created['id']}/", {"username": "different"}, format="json"
        )

        config.refresh_from_db()
        self.assertEqual(config.status, TenantEmailConfiguration.Status.UNVERIFIED)

    def test_editing_only_the_label_leaves_verification_intact(self):
        """Renaming a connection is not a reason to make a tenant re-test it."""
        created = self.create_config()
        config = TenantEmailConfiguration.objects.get()
        config.status = TenantEmailConfiguration.Status.VERIFIED
        config.save(update_fields=["status"])

        self.client_alex.patch(
            f"{URL}{created['id']}/", {"name": "Primary (SendGrid)"}, format="json"
        )

        config.refresh_from_db()
        self.assertEqual(config.status, TenantEmailConfiguration.Status.VERIFIED)


class ProviderPresetTests(EmailConfigurationTestCase):
    def test_preset_fills_the_connection_fields(self):
        created = self.create_config()
        self.assertEqual(created["host"], "smtp.sendgrid.net")
        self.assertEqual(created["port"], 587)
        self.assertTrue(created["use_tls"])
        self.assertFalse(created["use_ssl"])

    def test_an_explicit_value_beats_the_preset(self):
        created = self.create_config(port=2525)
        self.assertEqual(created["port"], 2525)
        self.assertEqual(created["host"], "smtp.sendgrid.net")

    def test_ssl_presets_set_the_flags_the_other_way(self):
        """Hostinger and Zoho are implicit-TLS on 465. Getting the pair backwards is
        a ValueError from inside Django's mail backend, not a clean 400."""
        for provider, name in (("HOSTINGER", "Hostinger"), ("ZOHO", "Zoho")):
            with self.subTest(provider=provider):
                created = self.create_config(provider=provider, name=name)
                self.assertEqual(created["port"], 465)
                self.assertTrue(created["use_ssl"])
                self.assertFalse(created["use_tls"])

    def test_providers_without_a_preset_host_must_supply_one(self):
        for provider in ("SES", "CUSTOM"):
            with self.subTest(provider=provider):
                response = self.client_alex.post(
                    URL, self.payload(provider=provider, name=provider), format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("host", str(response.data))

    def test_ses_accepts_an_explicit_regional_host(self):
        created = self.create_config(
            provider="SES", host="email-smtp.ap-south-1.amazonaws.com"
        )
        self.assertEqual(created["host"], "email-smtp.ap-south-1.amazonaws.com")

    def test_changing_provider_re_applies_the_new_preset(self):
        created = self.create_config()
        response = self.client_alex.patch(
            f"{URL}{created['id']}/", {"provider": "HOSTINGER"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["data"]["host"], "smtp.hostinger.com")
        self.assertEqual(response.data["data"]["port"], 465)

    def test_providers_endpoint_lists_every_preset_with_hints(self):
        response = self.client_alex.get(f"{URL}providers/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        entries = {row["value"]: row for row in response.data["data"]}

        self.assertEqual(
            set(entries), set(TenantEmailConfiguration.Provider.values)
        )
        for value, row in entries.items():
            with self.subTest(provider=value):
                self.assertTrue(row["username_hint"])
                self.assertTrue(row["password_hint"])

        # SendGrid's username is a fixed literal, which is the single most common
        # misconfiguration this endpoint exists to prevent.
        self.assertIn("apikey", entries["SENDGRID"]["username_hint"])

    def test_mailbox_providers_carry_a_send_limit_warning(self):
        response = self.client_alex.get(f"{URL}providers/")
        rows = response.data["data"]

        mailboxes = [r for r in rows if r["kind"] == providers.KIND_MAILBOX]
        self.assertTrue(mailboxes)
        for row in mailboxes:
            with self.subTest(provider=row["value"]):
                self.assertIsNotNone(row["warning"])

        outlook = next(r for r in rows if r["value"] == "OUTLOOK")
        self.assertIn("September 2025", outlook["warning"])


class SingleDefaultTests(EmailConfigurationTestCase):
    def test_the_first_configuration_becomes_the_default(self):
        first = self.create_config(name="First")
        self.assertTrue(first["is_default"])

        second = self.create_config(name="Second")
        self.assertFalse(second["is_default"])

    def test_set_default_promotes_one_and_demotes_the_other(self):
        first = self.create_config(name="First")
        second = self.create_config(name="Second")

        response = self.client_alex.post(f"{URL}{second['id']}/set-default/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["data"]["is_default"])

        defaults = TenantEmailConfiguration.objects.filter(
            tenant=self.tenant, is_default=True
        )
        self.assertEqual(defaults.count(), 1)
        self.assertEqual(str(defaults.first().id), second["id"])
        self.assertFalse(
            TenantEmailConfiguration.objects.get(pk=first["id"]).is_default
        )

    def test_is_default_in_the_request_body_is_ignored(self):
        """A writable flag lets a client clear it and leave the tenant with none, or
        set it on a second row and hit the unique index as a 500."""
        self.create_config(name="First")
        second = self.create_config(name="Second", is_default=True)
        self.assertFalse(second["is_default"])

        response = self.client_alex.patch(
            f"{URL}{second['id']}/", {"is_default": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["data"]["is_default"])

    def test_deleting_the_default_promotes_another(self):
        first = self.create_config(name="First")
        second = self.create_config(name="Second")

        response = self.client_alex.delete(f"{URL}{first['id']}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertTrue(
            TenantEmailConfiguration.objects.get(pk=second["id"]).is_default
        )

    def test_promotion_prefers_a_verified_configuration(self):
        """Promoting something that has never completed a test send hands the tenant
        a default that cannot actually send."""
        first = self.create_config(name="First")
        self.create_config(name="Unverified")
        verified = self.create_config(name="Verified")

        row = TenantEmailConfiguration.objects.get(pk=verified["id"])
        row.status = TenantEmailConfiguration.Status.VERIFIED
        row.save(update_fields=["status"])

        self.client_alex.delete(f"{URL}{first['id']}/")

        row.refresh_from_db()
        self.assertTrue(row.is_default)

    def test_deleting_the_last_configuration_leaves_no_default(self):
        only = self.create_config(name="Only")
        self.client_alex.delete(f"{URL}{only['id']}/")

        self.assertEqual(
            TenantEmailConfiguration.objects.filter(tenant=self.tenant).count(), 0
        )
        self.assertIsNone(TenantEmailConfiguration.ensure_default(self.tenant.id))

    def test_bulk_delete_promotes_a_replacement(self):
        first = self.create_config(name="First")
        second = self.create_config(name="Second")
        self.create_config(name="Third")

        response = self.client_alex.post(
            f"{URL}bulk_delete/", {"ids": [first["id"], second["id"]]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        remaining = TenantEmailConfiguration.objects.filter(tenant=self.tenant)
        self.assertEqual(remaining.count(), 1)
        self.assertTrue(remaining.first().is_default)

    def test_each_tenant_keeps_its_own_default(self):
        mine = self.create_config(name="Mine")
        theirs = self.create_config(client=self.client_bob, name="Theirs")

        self.assertTrue(mine["is_default"])
        self.assertTrue(theirs["is_default"])
        self.assertEqual(
            TenantEmailConfiguration.objects.filter(is_default=True).count(), 2
        )


class DisconnectTests(EmailConfigurationTestCase):
    """Disconnecting keeps the credentials but takes the configuration out of
    service. The trap it guards against: a disconnected row still satisfies the
    unique-default constraint perfectly well, and sends nothing."""

    def test_disconnect_takes_a_configuration_out_of_service(self):
        created = self.create_config(name="Primary")

        response = self.client_alex.post(f"{URL}{created['id']}/disconnect/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["data"]["is_active"])

        config = TenantEmailConfiguration.objects.get(pk=created["id"])
        self.assertFalse(config.is_active)
        # The credentials survive — that is the whole difference from deleting.
        self.assertTrue(config.has_password())

    def test_disconnecting_the_default_promotes_a_connected_replacement(self):
        first = self.create_config(name="First")
        second = self.create_config(name="Second")

        self.client_alex.post(f"{URL}{first['id']}/disconnect/")

        self.assertTrue(
            TenantEmailConfiguration.objects.get(pk=second["id"]).is_default
        )
        self.assertFalse(
            TenantEmailConfiguration.objects.get(pk=first["id"]).is_default
        )

    def test_promotion_on_disconnect_prefers_a_verified_configuration(self):
        first = self.create_config(name="First")
        self.create_config(name="Unverified")
        verified = self.create_config(name="Verified")

        row = TenantEmailConfiguration.objects.get(pk=verified["id"])
        row.status = TenantEmailConfiguration.Status.VERIFIED
        row.save(update_fields=["status"])

        self.client_alex.post(f"{URL}{first['id']}/disconnect/")

        row.refresh_from_db()
        self.assertTrue(row.is_default)

    def test_a_disconnected_configuration_is_never_promoted(self):
        first = self.create_config(name="First")
        spare = self.create_config(name="Spare")
        self.client_alex.post(f"{URL}{spare['id']}/disconnect/")

        self.client_alex.post(f"{URL}{first['id']}/disconnect/")

        # Nothing connected to promote, so the tenant's stated choice is kept
        # rather than demoted — but it is inactive, so nothing will send through it.
        first_row = TenantEmailConfiguration.objects.get(pk=first["id"])
        self.assertTrue(first_row.is_default)
        self.assertFalse(first_row.is_active)
        self.assertFalse(
            TenantEmailConfiguration.objects.get(pk=spare["id"]).is_default
        )

    def test_reconnect_restores_service_and_the_default(self):
        only = self.create_config(name="Only")
        self.client_alex.post(f"{URL}{only['id']}/disconnect/")

        response = self.client_alex.post(f"{URL}{only['id']}/reconnect/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["data"]["is_active"])
        self.assertTrue(response.data["data"]["is_default"])

    def test_reconnect_does_not_steal_the_default_from_a_working_one(self):
        first = self.create_config(name="First")
        spare = self.create_config(name="Spare")
        self.client_alex.post(f"{URL}{spare['id']}/disconnect/")

        self.client_alex.post(f"{URL}{spare['id']}/reconnect/")

        self.assertTrue(
            TenantEmailConfiguration.objects.get(pk=first["id"]).is_default
        )
        self.assertFalse(
            TenantEmailConfiguration.objects.get(pk=spare["id"]).is_default
        )

    def test_disconnect_is_idempotent(self):
        created = self.create_config(name="Primary")
        self.client_alex.post(f"{URL}{created['id']}/disconnect/")
        response = self.client_alex.post(f"{URL}{created['id']}/disconnect/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["data"]["is_active"])

    def test_is_active_in_the_request_body_is_ignored(self):
        """Otherwise a PATCH could disconnect the default and skip the promotion."""
        created = self.create_config(name="Primary")
        self.create_config(name="Spare")

        response = self.client_alex.patch(
            f"{URL}{created['id']}/", {"is_active": False}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["data"]["is_active"])
        self.assertTrue(response.data["data"]["is_default"])

    def test_another_tenant_cannot_disconnect_your_configuration(self):
        mine = self.create_config(name="Mine")
        response = self.client_bob.post(f"{URL}{mine['id']}/disconnect/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(TenantEmailConfiguration.objects.get(pk=mine["id"]).is_active)

    def test_a_disconnected_configuration_can_still_be_tested(self):
        """Proving a fix works before putting it back into service is the point."""
        created = self.create_config(name="Primary")
        self.client_alex.post(f"{URL}{created['id']}/disconnect/")

        with patch.object(mail_connection, "send_test_email", return_value=(True, "")):
            response = self.client_alex.post(f"{URL}{created['id']}/test/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        config = TenantEmailConfiguration.objects.get(pk=created["id"])
        self.assertEqual(config.status, TenantEmailConfiguration.Status.VERIFIED)
        self.assertFalse(config.is_active)


class NamingTests(EmailConfigurationTestCase):
    def test_duplicate_name_within_a_tenant_is_rejected(self):
        self.create_config(name="Primary")
        response = self.client_alex.post(URL, self.payload(name="Primary"), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data.get("errors", {}) or str(response.data))

    def test_the_same_name_in_a_different_tenant_is_fine(self):
        self.create_config(name="Primary")
        other = self.create_config(client=self.client_bob, name="Primary")
        self.assertEqual(other["name"], "Primary")

    def test_several_unnamed_configurations_are_allowed(self):
        self.create_config(name="")
        self.create_config(name="")
        self.assertEqual(
            TenantEmailConfiguration.objects.filter(tenant=self.tenant).count(), 2
        )


class IsolationTests(EmailConfigurationTestCase):
    def test_another_tenants_configuration_is_invisible(self):
        theirs = self.create_config(client=self.client_bob, name="Theirs")

        self.assertEqual(
            self.client_alex.get(f"{URL}{theirs['id']}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        listing = self.client_alex.get(URL)
        self.assertEqual(listing.data["data"], [])

    def test_another_tenants_configuration_cannot_be_promoted(self):
        self.create_config(name="Mine")
        theirs = self.create_config(client=self.client_bob, name="Theirs")

        response = self.client_alex.post(f"{URL}{theirs['id']}/set-default/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(
            TenantEmailConfiguration.objects.get(pk=theirs["id"]).is_default
        )

    def test_a_tenant_field_in_the_body_is_ignored(self):
        created = self.create_config(tenant=str(self.other_tenant.id))
        self.assertEqual(
            TenantEmailConfiguration.objects.get(pk=created["id"]).tenant_id,
            self.tenant.id,
        )

    def test_authentication_is_required(self):
        self.assertEqual(
            APIClient().get(URL).status_code, status.HTTP_401_UNAUTHORIZED
        )


class ConnectionValidationTests(EmailConfigurationTestCase):
    """Host validation runs for real here — the stub from the base class is stopped."""

    def setUp(self):
        super().setUp()
        patch.stopall()

    def post(self, **overrides):
        return self.client_alex.post(
            URL, self.payload(provider="CUSTOM", **overrides), format="json"
        )

    @override_settings(TENANT_SMTP_ALLOW_PRIVATE_HOSTS=False)
    def test_loopback_and_metadata_and_private_hosts_are_rejected(self):
        for host in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "localhost"):
            with self.subTest(host=host):
                response = self.post(host=host)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("host", str(response.data))

    @override_settings(TENANT_SMTP_ALLOW_PRIVATE_HOSTS=True)
    def test_local_development_can_opt_into_private_hosts(self):
        """Otherwise nobody can point at MailHog on localhost."""
        response = self.post(host="127.0.0.1", port=2525, use_tls=False, use_ssl=False)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @override_settings(TENANT_SMTP_ALLOW_PRIVATE_HOSTS=False)
    def test_an_unresolvable_host_is_rejected(self):
        response = self.post(host="no-such-host.invalid")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(TENANT_SMTP_ALLOW_PRIVATE_HOSTS=True)
    def test_port_25_and_service_ports_are_rejected(self):
        for port in (25, 6379, 5432):
            with self.subTest(port=port):
                response = self.post(host="127.0.0.1", port=port)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("port", str(response.data))

    @override_settings(TENANT_SMTP_ALLOW_PRIVATE_HOSTS=True)
    def test_both_tls_flags_is_a_400_not_a_500(self):
        response = self.post(host="127.0.0.1", use_tls=True, use_ssl=True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("use_ssl", str(response.data))

    @override_settings(TENANT_SMTP_ALLOW_PRIVATE_HOSTS=False)
    def test_plaintext_smtp_is_refused(self):
        response = self.post(host="127.0.0.1", use_tls=False, use_ssl=False)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class TestSendTests(EmailConfigurationTestCase):
    def setUp(self):
        super().setUp()
        self.created = self.create_config()
        self.test_url = f"{URL}{self.created['id']}/test/"

    def test_a_successful_send_verifies_the_configuration(self):
        with patch.object(mail_connection, "send_test_email", return_value=(True, "")):
            response = self.client_alex.post(self.test_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        config = TenantEmailConfiguration.objects.get()
        self.assertEqual(config.status, TenantEmailConfiguration.Status.VERIFIED)
        self.assertIsNotNone(config.verified_at)
        self.assertIsNotNone(config.last_tested_at)
        self.assertEqual(config.last_error, "")

    def test_the_test_goes_to_the_callers_own_address(self):
        """An endpoint that mails an arbitrary address on demand is a relay."""
        with patch.object(
            mail_connection, "send_test_email", return_value=(True, "")
        ) as send:
            self.client_alex.post(self.test_url, {"recipient": "victim@example.com"})

        _config, recipient = send.call_args[0]
        self.assertEqual(recipient, self.user.email)

    def test_a_failure_records_the_real_error(self):
        with patch.object(
            mail_connection,
            "send_test_email",
            return_value=(False, "535 Authentication failed"),
        ):
            response = self.client_alex.post(self.test_url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("535", str(response.data["errors"]))

        config = TenantEmailConfiguration.objects.get()
        self.assertEqual(config.status, TenantEmailConfiguration.Status.FAILED)
        self.assertIn("535", config.last_error)
        self.assertIsNone(config.verified_at)

    def test_testing_a_replacement_leaves_the_default_untouched(self):
        """The reason multiple configurations are worth supporting: a new provider
        can be proven before cutover rather than after."""
        replacement = self.create_config(name="Replacement", provider="POSTMARK")

        with patch.object(
            mail_connection, "send_test_email", return_value=(False, "nope")
        ):
            self.client_alex.post(f"{URL}{replacement['id']}/test/")

        default = TenantEmailConfiguration.objects.get(pk=self.created["id"])
        self.assertTrue(default.is_default)
        self.assertEqual(default.status, TenantEmailConfiguration.Status.UNVERIFIED)
        self.assertEqual(default.last_error, "")

    def test_the_test_endpoint_is_throttled(self):
        with patch.object(mail_connection, "send_test_email", return_value=(True, "")):
            codes = [
                self.client_alex.post(self.test_url).status_code for _ in range(6)
            ]
        self.assertEqual(codes[-1], status.HTTP_429_TOO_MANY_REQUESTS)

    def test_another_tenant_cannot_test_your_configuration(self):
        with patch.object(mail_connection, "send_test_email") as send:
            response = self.client_bob.post(self.test_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        send.assert_not_called()


class FailureMessageTests(APITestCase):
    """`describe_failure` is what a tenant reads when a test fails. Generic copy
    would be worthless — the difference between 'authentication failed' and
    'connection refused' is the whole diagnostic value."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-msg")
        self.config = TenantEmailConfiguration(
            tenant=self.tenant,
            host="smtp.sendgrid.net",
            port=587,
            from_email="bookings@acme.test",
        )

    def test_microsoft_auth_failure_explains_that_basic_auth_is_retired(self):
        self.config.host = "smtp.office365.com"
        message = mail_connection.describe_failure(
            self.config, smtplib.SMTPAuthenticationError(535, b"denied")
        )
        self.assertIn("September 2025", message)

    def test_a_custom_config_pointed_at_office365_gets_the_same_explanation(self):
        """Keyed on the host, not on the stored provider."""
        self.config.host = "mail.outlook.com"
        message = mail_connection.describe_failure(
            self.config, smtplib.SMTPAuthenticationError(535, b"denied")
        )
        self.assertIn("September 2025", message)

    def test_ordinary_auth_failure_mentions_the_username_convention(self):
        message = mail_connection.describe_failure(
            self.config, smtplib.SMTPAuthenticationError(535, b"denied")
        )
        self.assertIn("apikey", message)

    def test_a_refused_sender_points_at_provider_verification(self):
        message = mail_connection.describe_failure(
            self.config, smtplib.SMTPSenderRefused(550, b"denied", "bookings@acme.test")
        )
        self.assertIn("verified", message)

    def test_a_refused_recipient_does_not_condemn_the_configuration(self):
        """One bad address is not a broken credential."""
        self.assertFalse(
            mail_connection.is_permanent(smtplib.SMTPRecipientsRefused({}))
        )
        self.assertTrue(
            mail_connection.is_permanent(
                smtplib.SMTPAuthenticationError(535, b"denied")
            )
        )


@override_settings(SCHEDULING_TOKEN_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY)
class ModelInvariantTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-model")

    def make(self, **kwargs):
        defaults = {
            "tenant": self.tenant,
            "host": "smtp.example.com",
            "port": 587,
            "from_email": "a@acme.test",
        }
        defaults.update(kwargs)
        return TenantEmailConfiguration.objects.create(**defaults)

    def test_make_default_is_idempotent(self):
        config = self.make(name="One")
        config.make_default()
        config.make_default()
        self.assertEqual(
            TenantEmailConfiguration.objects.filter(
                tenant=self.tenant, is_default=True
            ).count(),
            1,
        )

    def test_promotion_demotes_the_previous_default(self):
        first = self.make(name="One")
        first.make_default()
        second = self.make(name="Two")
        second.make_default()

        first.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)

    def test_ensure_default_is_a_no_op_when_a_connected_one_already_exists(self):
        first = self.make(name="One")
        first.make_default()
        self.make(name="Two")

        self.assertEqual(
            TenantEmailConfiguration.ensure_default(self.tenant.id).pk, first.pk
        )

    def test_ensure_default_replaces_a_disconnected_default(self):
        """A disconnected default satisfies the unique constraint and sends
        nothing — the check has to ask 'usable?', not merely 'present?'."""
        first = self.make(name="One")
        first.make_default()
        second = self.make(name="Two")

        first.is_active = False
        first.save(update_fields=["is_active"])

        self.assertEqual(
            TenantEmailConfiguration.ensure_default(self.tenant.id).pk, second.pk
        )

    def test_ensure_default_skips_soft_deleted_rows(self):
        first = self.make(name="One")
        first.make_default()
        first.soft_delete()

        second = self.make(name="Two")
        promoted = TenantEmailConfiguration.ensure_default(self.tenant.id)
        self.assertEqual(promoted.pk, second.pk)

    def test_the_tls_pair_is_rejected_by_model_validation(self):
        from django.core.exceptions import ValidationError

        config = self.make(name="One", use_tls=True, use_ssl=True)
        with self.assertRaises(ValidationError):
            config.clean()
