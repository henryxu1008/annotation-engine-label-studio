import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0027_blind_assignment_exposure')]
    operations = [
        migrations.CreateModel(name='BlindDispatchReceipt', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('key', models.UUIDField()),
            ('request_hash', models.CharField(max_length=64)),
            ('actor_id', models.PositiveBigIntegerField()),
            ('assignment_id_frozen', models.PositiveBigIntegerField()),
            ('assignment', models.OneToOneField(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                to='tasks.taskassignment')),
            ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='projects.project')),
        ], options={'constraints': [models.UniqueConstraint(
            fields=['project', 'key'], name='blind_dispatch_project_key_unique',
        )]}),
    ]
