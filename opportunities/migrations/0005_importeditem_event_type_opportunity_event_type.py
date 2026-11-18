from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('opportunities', '0004_importeditem_source_alter_opportunity_source'),
    ]

    operations = [
        migrations.AddField(
            model_name='importeditem',
            name='event_type',
            field=models.CharField(blank=True, choices=[('seminar', 'Seminar'), ('lecture', 'Lecture'), ('workshop', 'Workshop'), ('conference', 'Conference'), ('competition', 'Competition'), ('other', 'Other event')], default='', max_length=20, verbose_name='Event format'),
        ),
        migrations.AddField(
            model_name='opportunity',
            name='event_type',
            field=models.CharField(blank=True, choices=[('seminar', 'Seminar'), ('lecture', 'Lecture'), ('workshop', 'Workshop'), ('conference', 'Conference'), ('competition', 'Competition'), ('other', 'Other event')], default='', max_length=20, verbose_name='Event format'),
        ),
    ]
