import django.core.validators
from django.db import migrations, models


def refuse_owner_loss(apps, schema_editor):
    storage = apps.get_model('io_storages', 'S3ExportStorage')
    operation = apps.get_model('io_storages', 'StorageOperation')
    link = apps.get_model('io_storages', 'S3ExportStorageLink')
    alias = schema_editor.connection.alias
    # v1 and v2 bindings have the same length; without an algorithm marker
    # a downgrade cannot safely identify which persisted records need the owner.
    if operation.objects.using(alias).exists() or link.objects.using(alias).filter(
        pinned_storage_binding__isnull=False
    ).exists():
        raise RuntimeError('Cannot reverse while storage operations or S3 version bindings exist.')
    if storage.objects.using(alias).exclude(
        models.Q(expected_bucket_owner__isnull=True) | models.Q(expected_bucket_owner='')
    ).exists():
        raise RuntimeError('Cannot reverse while S3 export bucket owner bindings are configured.')


class Migration(migrations.Migration):
    dependencies = [('io_storages', '0023_storage_operation')]

    operations = [
        migrations.AddField(
            model_name='s3exportstorage',
            name='expected_bucket_owner',
            field=models.CharField(
                blank=True, null=True, max_length=12,
                help_text='Expected AWS bucket owner account ID for owner-bound exports.',
                validators=[django.core.validators.RegexValidator(r'^[0-9]{12}$', 'Enter a 12-digit AWS account ID.')],
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_owner_loss),
    ]
