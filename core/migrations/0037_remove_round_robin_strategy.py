"""
Retire the ROUND_ROBIN assignment strategy.

Every ROUND_ROBIN event template becomes DIRECT, which means bookings for it now
require a named host (see core/services/bookings.py). Pooled auto-assignment and
leave-time auto-reassignment are gone with it.

IRREVERSIBLE (data): once a row is DIRECT there is no way to tell whether it was
originally ROUND_ROBIN, so the reverse of the data step is a no-op. Reversing the
migration restores the old *choices* but not the old *values*.
"""

from django.db import migrations, models


def forwards(apps, schema_editor):
    EventTemplate = apps.get_model("core", "EventTemplate")
    # Historical models get a plain Manager (neither ExceptDeletedManager nor
    # all_objects sets use_in_migrations), so this deliberately also rewrites
    # soft-deleted rows — a stale ROUND_ROBIN value would break
    # get_assignment_strategy_display() wherever those rows are still read.
    EventTemplate.objects.filter(assignment_strategy="ROUND_ROBIN").update(
        assignment_strategy="DIRECT"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0036_alter_calendarintegration_provider_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="eventtemplate",
            name="assignment_strategy",
            field=models.CharField(
                choices=[("COLLECTIVE", "Collective"), ("DIRECT", "Direct")],
                default="DIRECT",
                help_text=(
                    "How hosts are assigned when booking this event type. DIRECT "
                    "books the named host; COLLECTIVE books every skilled host as "
                    "one meeting."
                ),
                max_length=20,
            ),
        ),
        # UserEventSkill.weight existed solely as a round-robin selection bias and
        # was never read by any code path. Dropping the column with the feature.
        migrations.RemoveField(
            model_name="usereventskill",
            name="weight",
        ),
    ]
