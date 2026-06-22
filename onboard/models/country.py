import uuid
from django.db import models

class Country(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=10, unique=True, help_text="Country Code (e.g., IN, US)")
    name = models.CharField(max_length=100, db_index=True)

    class Meta:
        db_table = "onboard_country"
        ordering = ["name"]
        
    def __str__(self):
        return self.name
