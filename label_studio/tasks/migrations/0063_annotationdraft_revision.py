from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('tasks', '0062_submission_reviewdecision')]
    operations = [
        migrations.AddField(
            model_name='annotationdraft',
            name='revision',
            field=models.PositiveBigIntegerField(default=1, editable=False),
        ),
    ]
