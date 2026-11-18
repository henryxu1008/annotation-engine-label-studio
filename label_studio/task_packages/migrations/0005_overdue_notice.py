import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def preserve_pools(apps, schema_editor):
    if apps.get_model('task_packages', 'TaskPackagePool').objects.using(schema_editor.connection.alias).exists():
        raise RuntimeError('Cannot reverse package notices while pool configuration or claims exist.')


class Migration(migrations.Migration):
    dependencies = [('task_packages', '0004_pool_reopen_priority'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(
            name='TaskPackageOverdueNotice',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('claim_id_snapshot', models.PositiveBigIntegerField()),
                ('due_at', models.DateTimeField()),
                ('overdue_count', models.PositiveIntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('pool', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='overdue_notices', to='task_packages.taskpackagepool')),
                ('recipient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={'constraints': [models.UniqueConstraint(fields=('pool', 'claim_id_snapshot', 'due_at'), name='package_overdue_notice_unique')]},
        ),
        migrations.RunPython(migrations.RunPython.noop, preserve_pools),
    ]
