from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('opportunities', '0006_opportunity_allow_missing_details_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='importeditem',
            name='source',
            field=models.CharField(choices=[('manual', 'Manual'), ('notion', 'Notion'), ('slack', 'Slack'), ('gmail', 'Gmail'), ('email', 'Email'), ('other', 'Other')], default='notion', max_length=20),
        ),
        migrations.AlterField(
            model_name='opportunity',
            name='source',
            field=models.CharField(choices=[('manual', 'Manual'), ('notion', 'Notion'), ('slack', 'Slack'), ('gmail', 'Gmail'), ('email', 'Email'), ('other', 'Other')], default='manual', max_length=20),
        ),
    ]
