import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('opportunities', '0002_importeditem'),
    ]

    operations = [
        migrations.AddField(
            model_name='importeditem',
            name='employment_type',
            field=models.CharField(blank=True, choices=[('full_time', 'Full-time'), ('part_time', 'Part-time')], max_length=20),
        ),
        migrations.AddField(
            model_name='importeditem',
            name='work_mode',
            field=models.CharField(blank=True, choices=[('remote', 'Remote / online'), ('hybrid', 'Hybrid'), ('on_site', 'On-site / in person')], max_length=20),
        ),
        migrations.AddField(
            model_name='opportunity',
            name='employment_type',
            field=models.CharField(blank=True, choices=[('full_time', 'Full-time'), ('part_time', 'Part-time')], max_length=20),
        ),
        migrations.AddField(
            model_name='opportunity',
            name='work_mode',
            field=models.CharField(blank=True, choices=[('remote', 'Remote / online'), ('hybrid', 'Hybrid'), ('on_site', 'On-site / in person')], max_length=20),
        ),
        migrations.AlterField(
            model_name='importeditem',
            name='location',
            field=models.CharField(blank=True, help_text='City/country from the source. Internship and part-time are not locations.', max_length=200),
        ),
        migrations.AlterField(
            model_name='importeditem',
            name='opportunity',
            field=models.OneToOneField(blank=True, help_text='The opportunity record created from this import. Blank means no draft has been created yet.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='imported_item', to='opportunities.opportunity'),
        ),
        migrations.AlterField(
            model_name='opportunity',
            name='location',
            field=models.CharField(blank=True, help_text='Geographic location or explicit applicant location requirement; work arrangement is recorded separately.', max_length=200),
        ),
    ]
