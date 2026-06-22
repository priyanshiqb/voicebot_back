import uuid
from django.db import models
from django.db.models import Func, F, Value

class Place(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    country = models.ForeignKey('onboard.Country', on_delete=models.CASCADE, related_name='places')
    admin1 = models.ForeignKey('onboard.Admin1', on_delete=models.CASCADE, null=True, blank=True, related_name='places')
    admin2 = models.ForeignKey('onboard.Admin2', on_delete=models.CASCADE, null=True, blank=True, related_name='places')
    admin3 = models.ForeignKey('onboard.Admin3', on_delete=models.CASCADE, null=True, blank=True, related_name='places')
    
    postal_code = models.CharField(max_length=20, db_index=True)
    name = models.CharField(max_length=180, db_index=True)

    class Meta:
        db_table = "onboard_place"
        ordering = ["name"]
        indexes = [
            models.Index(
                Func(
                    F('postal_code'),
                    Value(' '),
                    Value(''),
                    function='REPLACE'
                ),
                name='idx_place_postal_stripped'
            ),
        ]

    def __str__(self):
        return f"{self.name} - {self.postal_code}"
