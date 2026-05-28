from django.db import models
from utils.models.base_model_core import BaseModelCore
from django.utils import timezone


class TempUser(BaseModelCore):
    """
    TempUser Model representing temporary/pending registration accounts.
    It separates unverified users from the main CustomUser authentication database.
    """
    email = models.EmailField(
        unique=True,
        help_text="Primary email address of the pending registrant."
    )
    phone_number = models.CharField(
        max_length=20,
        help_text="Phone number of the pending registrant."
    )
    password_hash = models.CharField(
        max_length=255,
        help_text="Encrypted password representation."
    )
    first_name = models.CharField(
        max_length=100,
        help_text="First name of the pending registrant."
    )
    last_name = models.CharField(
        max_length=100,
        help_text="Last name of the pending registrant."
    )
    business_name = models.CharField(
        max_length=255,
        help_text="Registered business/company name."
    )
    otp_code = models.CharField(
        max_length=6,
        blank=True,
        null=True,
        help_text="6-digit numeric verification OTP."
    )
    otp_created_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Timestamp of OTP generation for expiration checks."
    )
    is_email_verified = models.BooleanField(
        default=False,
        help_text="Flag indicating whether the registrant has successfully verified their OTP."
    )
    selected_plan = models.ForeignKey(
        "onboard.Plan",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pending_registrations",
        help_text="The subscription plan selected during signup."
    )

    class Meta:
        db_table = "onboard_temp_user"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})"

    def is_otp_expired(self):
        """
        Check if the generated OTP code is older than 10 minutes.
        """
        if not self.otp_created_at:
            return True
        expiry_limit = timezone.now() - timezone.timedelta(minutes=10)
        return self.otp_created_at < expiry_limit
