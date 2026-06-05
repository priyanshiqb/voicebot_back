import os
import json
from django.db import migrations
from django.conf import settings


def seed_languages_and_backfill(apps, schema_editor):
    Language = apps.get_model('onboard', 'Language')
    Tenant = apps.get_model('tenant', 'Tenant')
    CustomUser = apps.get_model('user', 'CustomUser')
    TempUser = apps.get_model('onboard', 'TempUser')

    json_path = os.path.join(settings.BASE_DIR, 'json_files', 'language.json')
    if not os.path.exists(json_path):
        return

    with open(json_path, 'r', encoding='utf-8') as f:
        fixture_data = json.load(f)

    for item in fixture_data.get('data', []):
        Language.objects.get_or_create(
            code=item['code'],
            defaults={'name': item['name'], 'is_active': True}
        )

    # Retrieve default en-IN language instance
    default_lang = Language.objects.filter(code='en-IN').first()
    if default_lang:
        # Backfill Tenant records
        Tenant.objects.filter(language__isnull=True).update(language=default_lang)
        # Backfill CustomUser records
        CustomUser.objects.filter(language__isnull=True).update(language=default_lang)
        # Backfill TempUser records
        TempUser.objects.filter(language__isnull=True).update(language=default_lang)


def rollback_seed_languages(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('onboard', '0010_language_tempuser_language'),
        ('tenant', '0004_tenant_language'),
        ('user', '0006_customuser_language'),
    ]

    operations = [
        migrations.RunPython(seed_languages_and_backfill, rollback_seed_languages),
    ]
