import uuid
from django.db import models

class Admin2(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    admin1 = models.ForeignKey('onboard.Admin1', on_delete=models.CASCADE, related_name='admin2_districts')
    code = models.CharField(max_length=20, blank=True, null=True)
    name = models.CharField(max_length=100, db_index=True)

    class Meta:
        db_table = "onboard_admin2"
        ordering = ["name"]

    def __str__(self):
        return self.name
