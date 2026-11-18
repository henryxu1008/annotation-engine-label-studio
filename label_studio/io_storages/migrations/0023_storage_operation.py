import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def refuse_lossy_reverse(apps, schema_editor):
    operation = apps.get_model('io_storages', 'StorageOperation')
    link = apps.get_model('io_storages', 'S3ExportStorageLink')
    project = apps.get_model('projects', 'Project')
    event = apps.get_model('projects', 'ProjectLifecycleEvent')
    alias = schema_editor.connection.alias
    if event.objects.using(alias).exists() or project.objects.using(alias).exclude(
        lifecycle_state='legacy', lifecycle_revision=0
    ).exists():
        raise RuntimeError('Cannot reverse storage admission migration while lifecycle state or events exist.')
    if operation.objects.using(schema_editor.connection.alias).exists() or link.objects.using(
        schema_editor.connection.alias
    ).filter(models.Q(pinned_version_id__isnull=False) | models.Q(pinned_key__isnull=False) | models.Q(pinned_storage_binding__isnull=False) | models.Q(pinned_object_token__isnull=False)).exists():
        raise RuntimeError('Cannot reverse storage admission migration while operation records or S3 version pins exist.')


class Migration(migrations.Migration):
    dependencies = [
        ('io_storages', '0022_normalize_localfiles_paths'),
        ('projects', '0036_project_lifecycle'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='StorageOperation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('storage_model', models.CharField(max_length=128)),
                ('storage_id', models.PositiveBigIntegerField()),
                ('operation', models.CharField(max_length=48)),
                ('lifecycle_revision', models.PositiveBigIntegerField()),
                ('subject_id', models.PositiveBigIntegerField(null=True)),
                ('target_key', models.TextField(null=True)),
                ('target_version_id', models.TextField(null=True)),
                ('target_storage_binding', models.CharField(max_length=64, null=True)),
                ('target_object_token', models.CharField(max_length=32, null=True)),
                ('effect_payload', models.JSONField(null=True)),
                ('next_dispatch_at', models.DateTimeField(null=True)),
                ('dispatch_attempts', models.PositiveIntegerField(default=0)),
                ('state', models.CharField(choices=[('queued', 'Queued'), ('running', 'Running'), ('uncertain', 'Uncertain'), ('completed', 'Completed'), ('reconciled', 'Reconciled'), ('cancelled', 'Cancelled')], default='running', max_length=16)),
                ('started_at', models.DateTimeField(auto_now_add=True)),
                ('finished_at', models.DateTimeField(null=True)),
                ('resolution_reason', models.TextField(blank=True, default='')),
                ('resolved_by_id_snapshot', models.BigIntegerField(null=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='storage_operations', to='projects.project')),
                ('resolved_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={'indexes': [models.Index(fields=['project', 'state'], name='storage_op_project_state_idx')]},
        ),
        migrations.AddField(model_name='s3exportstoragelink', name='pinned_version_id', field=models.TextField(blank=True, null=True)),
        migrations.AddField(model_name='s3exportstoragelink', name='pinned_key', field=models.TextField(blank=True, null=True)),
        migrations.AddField(model_name='s3exportstoragelink', name='pinned_storage_binding', field=models.CharField(blank=True, max_length=64, null=True)),
        migrations.AddField(model_name='s3exportstoragelink', name='pinned_object_token', field=models.CharField(blank=True, max_length=32, null=True)),
        migrations.RunPython(migrations.RunPython.noop, refuse_lossy_reverse),
    ]
