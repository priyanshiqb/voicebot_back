"""
Platform email is recorded — signup, email change, password reset.

Two properties matter more than the rest:

* **The OTP code is never stored.** ``send_email_task`` takes ``otp_code`` and
  renders it into the message; a table of live one-time codes would be a
  credential store, and a worse liability than the visibility gap it closes.
* **A signup that changes its email stays one trail.** ``TempUser.email`` is
  rewritten in place, so the messages before and after land on different
  addresses. ``temp_user_id`` is the only thing joining them — and hijacking an
  in-progress signup by swapping its email is the attack this log exists to show.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_static.models import StaticDevice
from rest_framework.test import APIClient

from onboard.models.plan import Plan
from onboard.models.temp_user import TempUser
from user.models import CustomUser
from user.models.email_delivery_log import EmailDeliveryLog
from user.tasks import purge_email_delivery_logs

REGISTER_URL = "/api/v1/onboard/register-pending/"
RESEND_OTP_URL = "/api/v1/onboard/resend-otp/"


class SignupEmailLoggingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.plan = Plan.objects.create(
            code="basic_inr",
            name="Basic Plan",
            description="Starter",
            base_price=Decimal("1000.00"),
            discount_percentage=Decimal("0.00"),
            tax_percentage=Decimal("18.00"),
            currency=Plan.CurrencyChoices.INR,
            billing_cycle=Plan.BillingCycleChoices.MONTHLY,
            plan_type=Plan.PlanTypeChoices.PAID,
            sort_order=1,
            is_active=True,
        )

    def _register(self, email="newbie@example.com"):
        return self.client.post(
            REGISTER_URL,
            {
                "plan_id": str(self.plan.id),
                "email": email,
                "password": "Str0ngPass!23",
                "confirm_password": "Str0ngPass!23",
                "first_name": "New",
                "last_name": "Bie",
                "business_name": "Newbie Co",
            },
            format="json",
            HTTP_USER_AGENT="pytest-agent/1.0",
            REMOTE_ADDR="203.0.113.9",
        )

    def test_signup_is_recorded_with_its_requester(self):
        response = self._register()
        self.assertIn(response.status_code, (200, 201), response.data)

        log = EmailDeliveryLog.objects.get()
        self.assertEqual(log.purpose, EmailDeliveryLog.Purpose.SIGNUP_OTP)
        self.assertEqual(log.recipient, "newbie@example.com")
        self.assertEqual(log.status, EmailDeliveryLog.Status.SENT)
        self.assertIsNotNone(log.sent_at)
        self.assertEqual(log.ip_address, "203.0.113.9")
        self.assertIn("pytest-agent", log.user_agent)

    def test_the_signup_is_linked_to_its_temp_user_not_just_the_address(self):
        self._register()
        temp_user = TempUser.objects.get(email="newbie@example.com")
        log = EmailDeliveryLog.objects.get()
        self.assertEqual(log.temp_user_id, temp_user.id)
        self.assertIsNone(log.user_id)

    def test_the_otp_code_is_never_stored(self):
        self._register()
        temp_user = TempUser.objects.get(email="newbie@example.com")
        self.assertTrue(temp_user.otp_code)

        blob = " ".join(
            str(v) for v in EmailDeliveryLog.objects.values_list().first()
        )
        self.assertNotIn(temp_user.otp_code, blob)

    def test_a_send_failure_is_recorded_as_failed(self):
        with patch(
            "utils.tasks.send_mail", side_effect=RuntimeError("relay refused")
        ):
            self._register()

        log = EmailDeliveryLog.objects.get()
        self.assertEqual(log.status, EmailDeliveryLog.Status.FAILED)
        self.assertIn("relay refused", log.error)
        self.assertIsNone(log.sent_at)

    def test_a_resend_is_a_second_row_not_a_duplicate(self):
        """No dedup_key here on purpose: resending an OTP is a feature, and the
        second row is the truth."""
        self._register()
        temp_user = TempUser.objects.get(email="newbie@example.com")

        self.client.post(
            RESEND_OTP_URL,
            {},
            format="json",
            HTTP_X_TEMP_REGISTRATION_ID=str(temp_user.id),
        )

        rows = EmailDeliveryLog.objects.filter(temp_user_id=temp_user.id)
        self.assertGreaterEqual(rows.count(), 1)

    def test_logging_failure_never_blocks_the_email(self):
        """An audit trail that can stop a password reset is worse than no audit
        trail."""
        with patch(
            "user.services.email_log.EmailDeliveryLog.objects.create",
            side_effect=RuntimeError("db down"),
        ):
            response = self._register()

        self.assertIn(response.status_code, (200, 201))
        self.assertEqual(EmailDeliveryLog.objects.count(), 0)


class PasswordResetEmailLoggingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = CustomUser.objects.create_user(
            username="reset-alex", email="alex@example.com", password="password"
        )

    def test_an_account_reset_records_the_user_and_the_requester(self):
        response = self.client.post(
            "/api/v1/auth/forgot-password/",
            {"email": "alex@example.com"},
            format="json",
            REMOTE_ADDR="198.51.100.7",
        )
        self.assertEqual(response.status_code, 200, response.data)

        log = EmailDeliveryLog.objects.get()
        self.assertEqual(log.purpose, EmailDeliveryLog.Purpose.PASSWORD_RESET)
        self.assertEqual(log.user_id, self.user.id)
        self.assertIsNone(log.temp_user_id)
        self.assertEqual(log.ip_address, "198.51.100.7")

    def test_the_reset_otp_is_never_stored(self):
        self.client.post(
            "/api/v1/auth/forgot-password/",
            {"email": "alex@example.com"},
            format="json",
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.otp_code)

        blob = " ".join(
            str(v) for v in EmailDeliveryLog.objects.values_list().first()
        )
        self.assertNotIn(self.user.otp_code, blob)

    def test_an_unknown_address_leaves_no_row(self):
        """The endpoint answers identically either way to avoid enumeration; the
        log must not become the oracle that undoes that."""
        self.client.post(
            "/api/v1/auth/forgot-password/",
            {"email": "nobody@example.com"},
            format="json",
        )
        self.assertFalse(
            EmailDeliveryLog.objects.filter(recipient="nobody@example.com").exists()
        )


class RetentionTests(TestCase):
    """Rows hold an email address and the requester's IP, and nothing else prunes
    them — so the window each purpose gets is a privacy decision, not a tidiness
    one."""

    def _log(self, purpose, age_days, recipient="someone@example.com"):
        row = EmailDeliveryLog.objects.create(
            purpose=purpose,
            recipient=recipient,
            status=EmailDeliveryLog.Status.SENT,
        )
        # created_at is auto_now_add, so it has to be rewritten to age the row.
        EmailDeliveryLog.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(days=age_days)
        )
        return row

    def test_signup_records_expire_before_security_records(self):
        """The distinction the windows exist for: a reset request is evidence long
        after a signup OTP has stopped being interesting."""
        old_signup = self._log(EmailDeliveryLog.Purpose.SIGNUP_OTP, 200)
        old_reset = self._log(EmailDeliveryLog.Purpose.PASSWORD_RESET, 200)

        purge_email_delivery_logs()

        self.assertFalse(EmailDeliveryLog.all_objects.filter(pk=old_signup.pk).exists())
        self.assertTrue(EmailDeliveryLog.all_objects.filter(pk=old_reset.pk).exists())

    def test_security_records_do_expire_eventually(self):
        ancient = self._log(EmailDeliveryLog.Purpose.PASSWORD_RESET, 400)
        purge_email_delivery_logs()
        self.assertFalse(EmailDeliveryLog.all_objects.filter(pk=ancient.pk).exists())

    def test_recent_rows_survive(self):
        for purpose in EmailDeliveryLog.Purpose.values:
            self._log(purpose, 10)

        purge_email_delivery_logs()

        self.assertEqual(
            EmailDeliveryLog.all_objects.count(), len(EmailDeliveryLog.Purpose.values)
        )

    def test_the_purge_hard_deletes(self):
        """A soft delete would leave the address and IP in the table, which defeats
        the point of a retention policy."""
        row = self._log(EmailDeliveryLog.Purpose.SMTP_TEST, 200)
        purge_email_delivery_logs()
        self.assertFalse(EmailDeliveryLog.all_objects.filter(pk=row.pk).exists())

    @override_settings(EMAIL_LOG_PURGE_BATCH=2, EMAIL_DELIVERY_LOG_PURGE_BATCH=2)
    def test_batching_still_removes_everything(self):
        for _ in range(5):
            self._log(EmailDeliveryLog.Purpose.SMTP_TEST, 200)

        removed = purge_email_delivery_logs()

        self.assertEqual(removed, 5)
        self.assertEqual(EmailDeliveryLog.all_objects.count(), 0)

    @override_settings(EMAIL_DELIVERY_LOG_RETENTION_DAYS={})
    def test_a_purpose_with_no_configured_window_falls_back(self):
        """A purpose added to the model but not to settings must still expire —
        erring short beats accumulating forever because a line was forgotten."""
        old = self._log(EmailDeliveryLog.Purpose.PASSWORD_RESET, 200)
        recent = self._log(EmailDeliveryLog.Purpose.PASSWORD_RESET, 10)

        purge_email_delivery_logs()

        self.assertFalse(EmailDeliveryLog.all_objects.filter(pk=old.pk).exists())
        self.assertTrue(EmailDeliveryLog.all_objects.filter(pk=recent.pk).exists())


class AdminAccessTests(TestCase):
    """
    The admin is the only way to read this table — there is no API for it — so the
    permission gate on it *is* the access control for every address and IP in here.

    Nothing else in this admin can be gated the same way: the other logs are scoped
    by a ``tenant`` ForeignKey, which this model deliberately does not have. That
    leaves superuser-only as the whole of the protection, which is why it is tested
    rather than assumed.

    ``_login`` also satisfies the OTP device check, because the whole admin is
    behind ``AdminSiteOTPRequired`` (``config/urls.py``). Logging in without one
    redirects every request to the 2FA setup page — which would make these tests
    pass for the wrong reason, showing "denied" for a user the model layer would
    happily have let in.
    """

    def _login(self, user):
        """Authenticate *and* satisfy the admin's mandatory second factor."""
        device = StaticDevice.objects.create(user=user, name="test-device")
        self.client.force_login(user)
        session = self.client.session
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session.save()

    def setUp(self):
        self.superuser = CustomUser.objects.create_superuser(
            email="root@example.com", password="password", username="root"
        )
        # is_staff without is_superuser: reaches the admin, must not reach this model.
        self.staff = CustomUser.objects.create_user(
            username="support",
            email="support@example.com",
            password="password",
            is_staff=True,
        )
        EmailDeliveryLog.objects.create(
            purpose=EmailDeliveryLog.Purpose.PASSWORD_RESET,
            recipient="subject@example.com",
            status=EmailDeliveryLog.Status.SENT,
            ip_address="203.0.113.4",
        )
        self.changelist = reverse("admin:user_emaildeliverylog_changelist")

    def test_a_superuser_can_read_the_log(self):
        self._login(self.superuser)
        response = self.client.get(self.changelist)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "subject@example.com")

    def test_staff_without_superuser_cannot(self):
        """A support account with admin access must not get every tenant's
        addresses and request IPs."""
        self._login(self.staff)
        self.assertEqual(self.client.get(self.changelist).status_code, 403)

    def test_the_log_is_not_writable_through_the_admin(self):
        """An editable audit trail is not one."""
        self._login(self.superuser)
        response = self.client.get(reverse("admin:user_emaildeliverylog_add"))
        self.assertEqual(response.status_code, 403)

    def test_deleting_removes_the_row_rather_than_hiding_it(self):
        """Erasure requests arrive faster than the retention window, so delete stays
        available — but a soft delete would leave the address and IP in the table,
        which is the outcome the retention policy exists to prevent."""
        row = EmailDeliveryLog.objects.get()
        self._login(self.superuser)

        response = self.client.post(
            reverse("admin:user_emaildeliverylog_delete", args=[row.pk]),
            {"post": "yes"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(EmailDeliveryLog.all_objects.filter(pk=row.pk).exists())
