from django.db import models
from utils.models.base_model_core import BaseModelCore


def get_default_language():
    """
    Failsafe callable default to retrieve the 'en-IN' language database instance.
    Returns None if the database hasn't been seeded yet.
    """
    from onboard.models.language import Language
    try:
        obj = Language.objects.filter(code="en-IN").first()
        return obj.pk if obj else None
    except Exception:
        return None


class Language(BaseModelCore):
    code = models.CharField(
        max_length=50,
        unique=True,
        help_text="Standard language code (e.g., en-US, hi)"
    )
    name = models.CharField(
        max_length=255,
        help_text="Human-readable language label (e.g., English (US), Hindi)"
    )

    class Meta(BaseModelCore.Meta):
        db_table = "onboard_language"
        verbose_name = "Language"
        verbose_name_plural = "Languages"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"
