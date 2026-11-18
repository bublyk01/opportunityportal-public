from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('opportunities', '0005_importeditem_event_type_opportunity_event_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='opportunity',
            name='allow_missing_details',
            field=models.BooleanField(default=False, help_text='Allow organization or opportunity type to be missing after a staff review. A title and all other validation are still required.'),
        ),
        migrations.AlterField(
            model_name='opportunity',
            name='organization',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AlterField(
            model_name='opportunity',
            name='type',
            field=models.CharField(blank=True, choices=[('internship', 'Internship'), ('job', 'Job'), ('event', 'Event')], max_length=20),
        ),
    ]
