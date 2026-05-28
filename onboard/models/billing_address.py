from django.db import models
from utils.models.base_model_core import BaseModelCore
from django.conf import settings


class BillingAddress(BaseModelCore):
    """
    BillingAddress Model representing tax-compliant billing invoice address details.
    Saves details for both TempUser during onboarding and promotes to CustomUser upon payment.
    """
    temp_user = models.ForeignKey(
        "onboard.TempUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_addresses",
        help_text="Reference to the temporary pending user registration."
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_addresses",
        help_text="Reference to the active custom user account."
    )
    line1 = models.CharField(
        max_length=255,
        help_text="Address Line 1 (Street, Building, Suite)."
    )
    city = models.CharField(
        max_length=100,
        help_text="City name."
    )
    state = models.CharField(
        max_length=100,
        help_text="State or Province name."
    )
    zip_code = models.CharField(
        max_length=20,
        help_text="ZIP or Postal Code."
    )
    country = models.CharField(
        max_length=100,
        default="IN",
        help_text="Country name or ISO country code."
    )
    gstin = models.CharField(
        max_length=15,
        blank=True,
        null=True,
        help_text="Optional Business Tax Identification Number (e.g. GSTIN in India)."
    )

    class Meta:
        db_table = "onboard_billing_address"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.line1}, {self.city}, {self.state} ({self.country})"
