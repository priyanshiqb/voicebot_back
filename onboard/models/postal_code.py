import uuid
from django.db import models
from django.db.models import Func, F, Value


class PostalCode(models.Model):
    """
    PostalCode Master Reference Model.
    Stores Geonames postal code and administrative boundary mapping for all countries.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )
    is_active = models.BooleanField(default=True)
    country_code = models.CharField(
        max_length=10,
        db_index=True,
        help_text="ISO 3166-1 2-letter country code (e.g., AE, US, IN)"
    )
    postal_code = models.CharField(
        max_length=20,
        db_index=True,
        help_text="Postal code / ZIP code string (e.g., 32867 98458, 90210, 110001)"
    )
    place_name = models.CharField(
        max_length=180,
        db_index=True,
        help_text="Human-readable city, suburb, or neighborhood place name (e.g., Al Wuheida)"
    )
    admin_name1 = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="First-level administrative division name, typically the State, Province, or Emirate (e.g., California, Dubai)"
    )
    admin_code1 = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="First-level administrative division code/abbreviation (e.g., CA, 03)"
    )
    admin_name2 = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Second-level administrative division name, typically the County, District, or Municipality (e.g., Los Angeles, Al Wuheida)"
    )
    admin_code2 = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Second-level administrative division code (e.g., 132)"
    )
    admin_name3 = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Third-level administrative division name, typically the Sub-district or Community"
    )
    admin_code3 = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Third-level administrative division code"
    )
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="Estimated latitude coordinate in WGS84 format (e.g., 25.293300)"
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="Estimated longitude coordinate in WGS84 format (e.g., 55.339900)"
    )
    accuracy = models.IntegerField(
        null=True,
        blank=True,
        help_text="Positional accuracy index from 1 (estimated) to 6 (centroid)"
    )
    class Meta:
        db_table = "onboard_postal_code"
        verbose_name = "Postal Code"
        verbose_name_plural = "Postal Codes"
        ordering = ["country_code", "postal_code", "place_name"]
        indexes = [
            models.Index(fields=["country_code", "postal_code"]),
            models.Index(fields=["postal_code"]),
            models.Index(fields=["place_name"]),
            models.Index(fields=["latitude", "longitude"]),
            models.Index(
                Func(
                    F('postal_code'),
                    Value(' '),
                    Value(''),
                    function='REPLACE'
                ),
                name='idx_postal_code_stripped'
            ),
        ]

    @property
    def coordinates(self) -> str:
        if self.latitude is not None and self.longitude is not None:
            return f"lat: {self.latitude:.6f}, lon: {self.longitude:.6f}"
        return None

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.postal_code} - {self.place_name} ({self.country_code})"
