import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def preserve_receipts(apps, schema_editor):
    if apps.get_model('task_packages', 'TaskPackageBatch').objects.using(schema_editor.connection.alias).exists():
        raise RuntimeError('Cannot reverse package batches while batch receipts exist.')


class Migration(migrations.Migration):
    dependencies = [
        ('task_packages', '0007_pool_allocation_notices'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name='TaskPackageBatch',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('creator_principal', models.CharField(max_length=512)),
                ('idempotency_key', models.UUIDField()),
                ('request_hash', models.CharField(max_length=64)),
                ('receipt', models.JSONField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                (
                    'created_by',
                    models.ForeignKey(
                        null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    'project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='package_batches',
                        to='projects.project',
                    ),
                ),
            ],
            options={
                'constraints': [
                    models.UniqueConstraint(
                        fields=('project', 'idempotency_key'), name='package_batch_project_key_unique'
                    )
                ]
            },
        ),
        migrations.RunPython(migrations.RunPython.noop, preserve_receipts),
    ]
