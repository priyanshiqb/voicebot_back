from django.db import models
from utils.models.base_model_core import BaseModelCore

def get_default_timezone():
    """
    Failsafe callable default to retrieve the UTC timezone database instance.
    Returns None if the database hasn't been seeded yet.
    """
    from onboard.models.timezone import Timezone
    try:
        obj = Timezone.objects.filter(timezone="UTC").first()
        return obj.pk if obj else None
    except Exception:
        return None

class Timezone(BaseModelCore):
    offset = models.CharField(
        max_length=50, 
        help_text="Timezone UTC offset (e.g., UTC-12:00)"
    )
    region = models.CharField(
        max_length=255, 
        help_text="Associated regions/cities (e.g., London, Dublin)"
    )
    timezone = models.CharField(
        max_length=100, 
        unique=True, 
        help_text="Standard IANA timezone identifier (e.g., Europe/London)"
    )

    class Meta(BaseModelCore.Meta):
        db_table = "onboard_timezone"
        verbose_name = "Timezone"
        verbose_name_plural = "Timezones"
        ordering = ["timezone"]

    def __str__(self):
        return f"{self.timezone} ({self.offset})"
