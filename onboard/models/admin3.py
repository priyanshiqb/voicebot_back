import uuid
from django.db import models

class Admin3(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    admin2 = models.ForeignKey('onboard.Admin2', on_delete=models.CASCADE, related_name='admin3_subdistricts')
    code = models.CharField(max_length=20, blank=True, null=True)
    name = models.CharField(max_length=100, db_index=True)

    class Meta:
        db_table = "onboard_admin3"
        ordering = ["name"]

    def __str__(self):
        return self.name
