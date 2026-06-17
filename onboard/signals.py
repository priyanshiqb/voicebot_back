from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from onboard.models import TempUser
from utils.bloom_filter import RedisBloomFilter

User = get_user_model()


@receiver(post_save, sender=TempUser)
def sync_temp_user_email(sender, instance, created, **kwargs):
    if created and instance.email:
        RedisBloomFilter().add(instance.email)


@receiver(post_save, sender=User)
def sync_active_user_email(sender, instance, created, **kwargs):
    if created and instance.email:
        RedisBloomFilter().add(instance.email)
