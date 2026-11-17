import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def refuse_phase_loss(apps, schema_editor):
    operation = apps.get_model('io_storages', 'StorageOperation')
    recovery = apps.get_model('io_storages', 'StorageOperationRecovery')
    alias = schema_editor.connection.alias
    project = apps.get_model('projects', 'Project')
    event = apps.get_model('projects', 'ProjectLifecycleEvent')
    storage = apps.get_model('io_storages', 'S3ExportStorage')
    link = apps.get_model('io_storages', 'S3ExportStorageLink')
    if event.objects.using(alias).exists() or project.objects.using(alias).exclude(
        lifecycle_state='legacy', lifecycle_revision=0
    ).exists():
        raise RuntimeError('Cannot reverse while lifecycle state or events exist.')
    if operation.objects.using(alias).exists() or recovery.objects.using(alias).exists() or link.objects.using(alias).filter(
        pinned_storage_binding__isnull=False
    ).exists():
        raise RuntimeError('Cannot reverse while storage operations or S3 version bindings exist.')
    if storage.objects.using(alias).exclude(
        models.Q(expected_bucket_owner__isnull=True) | models.Q(expected_bucket_owner='')
    ).exists():
        raise RuntimeError('Cannot reverse while S3 export bucket owner bindings are configured.')


class Migration(migrations.Migration):
    dependencies = [
        ('io_storages', '0024_s3_export_expected_owner'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='storageoperation',
            name='effect_phase',
            field=models.CharField(max_length=32, null=True),
        ),
        migrations.AddField(
            model_name='storageoperation',
            name='effect_claim_token',
            field=models.UUIDField(null=True),
        ),
        migrations.CreateModel(
            name='StorageOperationRecovery',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('claim_token', models.UUIDField()),
                ('reason', models.TextField()),
                ('actor_id_snapshot', models.BigIntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('operation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='recoveries', to='io_storages.storageoperation')),
            ],
            options={'constraints': [models.UniqueConstraint(fields=('operation', 'claim_token'), name='unique_storage_recovery_claim')]},
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_phase_loss),
    ]
