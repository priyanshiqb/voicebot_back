from django.db.models.signals import post_save, pre_save, post_delete
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from onboard.models import TempUser
from utils.bloom_filter import RedisBloomFilter

User = get_user_model()


@receiver(pre_save, sender=TempUser)
def track_temp_user_email_change(sender, instance, **kwargs):
    if instance.pk:
        try:
            old_email = sender.all_objects.get(pk=instance.pk).email
            if old_email and old_email.lower() != instance.email.lower():
                instance._old_email = old_email
        except sender.DoesNotExist:
            pass


@receiver(pre_save, sender=User)
def track_user_email_change(sender, instance, **kwargs):
    if instance.pk:
        try:
            old_email = sender.all_objects.get(pk=instance.pk).email
            if old_email and old_email.lower() != instance.email.lower():
                instance._old_email = old_email
        except sender.DoesNotExist:
            pass


@receiver(post_save, sender=TempUser)
def sync_temp_user_email(sender, instance, **kwargs):
    bf = RedisBloomFilter()
    old_email = getattr(instance, "_old_email", None)
    if old_email:
        bf.remove(old_email)

    if instance.is_deleted:
        bf.remove(instance.email)
    elif instance.email:
        bf.add(instance.email)


@receiver(post_save, sender=User)
def sync_active_user_email(sender, instance, **kwargs):
    bf = RedisBloomFilter()
    old_email = getattr(instance, "_old_email", None)
    if old_email:
        bf.remove(old_email)

    if instance.email:
        bf.add(instance.email)


@receiver(post_delete, sender=TempUser)
def remove_deleted_temp_user_email(sender, instance, **kwargs):
    if instance.email:
        RedisBloomFilter().remove(instance.email)


@receiver(post_delete, sender=User)
def remove_deleted_user_email(sender, instance, **kwargs):
    if instance.email:
        RedisBloomFilter().remove(instance.email)
