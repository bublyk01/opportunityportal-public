import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Opportunity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200)),
                ('organization', models.CharField(max_length=200)),
                ('type', models.CharField(choices=[('internship', 'Internship'), ('job', 'Job'), ('event', 'Event')], max_length=20)),
                ('description', models.TextField(blank=True)),
                ('location', models.CharField(blank=True, max_length=200)),
                ('deadline', models.DateField(blank=True, null=True)),
                ('source', models.CharField(choices=[('manual', 'Manual'), ('notion', 'Notion'), ('email', 'Email'), ('other', 'Other')], default='manual', max_length=20)),
                ('source_url', models.URLField(blank=True, help_text='Optional link to the original posting. HTTP or HTTPS only.', max_length=500, validators=[django.core.validators.URLValidator(schemes=['http', 'https'])])),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('published', 'Published'), ('archived', 'Archived')], default='draft', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-created_at', '-pk'],
            },
        ),
    ]
