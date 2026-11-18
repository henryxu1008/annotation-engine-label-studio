import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def prevent_receipt_loss(apps, schema_editor):
    if apps.get_model('task_packages', 'TaskPackageDispatch').objects.using(schema_editor.connection.alias).exists():
        raise RuntimeError('Cannot reverse package dispatch while committed receipts exist.')


class Migration(migrations.Migration):
    dependencies = [
        ('task_packages', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TaskPackageDispatch',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('assignee_id_snapshot', models.PositiveBigIntegerField()),
                ('actor_id_snapshot', models.PositiveBigIntegerField()),
                ('creator_principal', models.CharField(max_length=512)),
                ('idempotency_key', models.UUIDField()),
                ('request_hash', models.CharField(max_length=64)),
                ('assignment_ids', models.JSONField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('assignee', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('package', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='dispatches', to='task_packages.taskpackage')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='projects.project')),
            ],
            options={'constraints': [models.UniqueConstraint(fields=('project', 'idempotency_key'), name='package_dispatch_key_unique')]},
        ),
        migrations.RunPython(migrations.RunPython.noop, prevent_receipt_loss),
    ]
