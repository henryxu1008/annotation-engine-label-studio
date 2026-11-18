from django.db import migrations, models
import django.db.models.deletion


def prevent_history_loss(apps, schema_editor):
    if apps.get_model('task_packages', 'TaskPackagePool').objects.using(schema_editor.connection.alias).exists():
        raise RuntimeError('Cannot reverse pool reopening while pool configuration or claims exist.')


class Migration(migrations.Migration):
    dependencies = [('task_packages', '0003_package_pool')]
    operations = [
        migrations.AddField(model_name='taskpackagepool', name='priority', field=models.PositiveSmallIntegerField(default=0)),
        migrations.CreateModel(
            name='TaskPackageClaimHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('original_claim_id', models.PositiveBigIntegerField()),
                ('actor_id_snapshot', models.PositiveBigIntegerField()),
                ('principal', models.CharField(max_length=512)),
                ('idempotency_key', models.UUIDField()),
                ('assignment_ids', models.JSONField()),
                ('claimed_at', models.DateTimeField()),
                ('reopened_at', models.DateTimeField(auto_now_add=True)),
                ('reopened_by_id_snapshot', models.PositiveBigIntegerField()),
                ('reopened_by_principal', models.CharField(max_length=512)),
                ('reopen_key', models.UUIDField()),
                ('reopen_revision', models.PositiveIntegerField()),
                ('pool', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='claim_history', to='task_packages.taskpackagepool')),
            ],
            options={'constraints': [
                models.UniqueConstraint(fields=('pool', 'original_claim_id'), name='package_history_claim_unique'),
                models.UniqueConstraint(fields=('pool', 'reopen_key'), name='package_history_reopen_key_unique'),
            ]},
        ),
        migrations.RunPython(migrations.RunPython.noop, prevent_history_loss),
    ]
