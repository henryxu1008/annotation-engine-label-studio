import django.db.models.deletion
from django.db import migrations, models


def reject_ambiguous_legacy_receipts(apps, schema_editor):
    alias = schema_editor.connection.alias
    dispatch = apps.get_model('task_packages', 'TaskPackageDispatch')
    if dispatch.objects.using(alias).filter(package__pool__isnull=False).exists():
        raise RuntimeError(
            'Cannot migrate legacy package pool dispatch receipts without a verified allocation reconciliation.'
        )


def preserve_allocations(apps, schema_editor):
    alias = schema_editor.connection.alias
    if (
        apps.get_model('task_packages', 'TaskPackagePool').objects.using(alias).exists()
        or apps.get_model('task_packages', 'TaskPackageOverdueNotice')
        .objects.using(alias).filter(dispatch_id_snapshot__isnull=False).exists()
    ):
        raise RuntimeError('Cannot reverse package allocation while pool configuration or notices exist.')


class Migration(migrations.Migration):
    dependencies = [('task_packages', '0006_pool_automation')]

    operations = [
        migrations.RunPython(reject_ambiguous_legacy_receipts, migrations.RunPython.noop),
        migrations.AddField(
            model_name='taskpackagepool',
            name='allocation',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT,
                related_name='allocated_pools', to='task_packages.taskpackagedispatch',
            ),
        ),
        migrations.AlterField(
            model_name='taskpackageoverduenotice', name='claim_id_snapshot',
            field=models.PositiveBigIntegerField(null=True),
        ),
        migrations.AddField(
            model_name='taskpackageoverduenotice', name='dispatch_id_snapshot',
            field=models.PositiveBigIntegerField(null=True),
        ),
        migrations.AddConstraint(
            model_name='taskpackageoverduenotice',
            constraint=models.UniqueConstraint(
                fields=('pool', 'dispatch_id_snapshot', 'due_at'), name='package_dispatch_notice_unique',
            ),
        ),
        migrations.AddConstraint(
            model_name='taskpackageoverduenotice',
            constraint=models.CheckConstraint(
                condition=(models.Q(claim_id_snapshot__isnull=False, dispatch_id_snapshot__isnull=True)
                           | models.Q(claim_id_snapshot__isnull=True, dispatch_id_snapshot__isnull=False)),
                name='package_notice_one_source',
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, preserve_allocations),
    ]
