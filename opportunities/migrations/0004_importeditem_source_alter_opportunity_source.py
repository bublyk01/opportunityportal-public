from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('opportunities', '0003_importeditem_employment_type_importeditem_work_mode_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='importeditem',
            name='source',
            field=models.CharField(choices=[('manual', 'Manual'), ('notion', 'Notion'), ('slack', 'Slack'), ('email', 'Email'), ('other', 'Other')], default='notion', max_length=20),
        ),
        migrations.AlterField(
            model_name='opportunity',
            name='source',
            field=models.CharField(choices=[('manual', 'Manual'), ('notion', 'Notion'), ('slack', 'Slack'), ('email', 'Email'), ('other', 'Other')], default='manual', max_length=20),
        ),
    ]
