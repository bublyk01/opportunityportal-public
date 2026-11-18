import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('opportunities', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='ImportedItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_key', models.CharField(max_length=64)),
                ('external_id', models.CharField(max_length=32)),
                ('source_url', models.URLField(max_length=500)),
                ('source_payload', models.JSONField(default=dict)),
                ('source_hash', models.CharField(max_length=64)),
                ('title', models.CharField(blank=True, max_length=200)),
                ('organization', models.CharField(blank=True, max_length=200)),
                ('type', models.CharField(blank=True, choices=[('internship', 'Internship'), ('job', 'Job'), ('event', 'Event')], max_length=20)),
                ('description', models.TextField(blank=True)),
                ('location', models.CharField(blank=True, max_length=200)),
                ('deadline', models.DateField(blank=True, null=True)),
                ('notes', models.TextField(blank=True)),
                ('review_required', models.BooleanField(default=True)),
                ('first_seen', models.DateTimeField(auto_now_add=True)),
                ('last_seen', models.DateTimeField(default=django.utils.timezone.now)),
                ('opportunity', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='imported_item', to='opportunities.opportunity')),
            ],
            options={
                'ordering': ['-first_seen', '-pk'],
                'constraints': [models.UniqueConstraint(fields=('source_key', 'external_id'), name='unique_imported_source_item')],
            },
        ),
    ]
