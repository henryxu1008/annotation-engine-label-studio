import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def prevent_pool_loss(apps, schema_editor):
    pool = apps.get_model('task_packages', 'TaskPackagePool')
    if pool.objects.using(schema_editor.connection.alias).exists():
        raise RuntimeError('Cannot reverse package pools while pool configuration or claims exist.')


class Migration(migrations.Migration):
    dependencies = [
        ('task_packages', '0002_taskpackagedispatch'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TaskPackagePool',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('enabled', models.BooleanField(default=True)),
                ('due_at', models.DateTimeField(blank=True, null=True)),
                ('revision', models.PositiveIntegerField(default=1)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('candidates', models.ManyToManyField(related_name='+', to=settings.AUTH_USER_MODEL)),
                ('package', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='pool', to='task_packages.taskpackage')),
            ],
        ),
        migrations.CreateModel(
            name='TaskPackageClaim',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('actor_id_snapshot', models.PositiveBigIntegerField()),
                ('principal', models.CharField(max_length=512)),
                ('idempotency_key', models.UUIDField()),
                ('assignment_ids', models.JSONField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('pool', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='claim', to='task_packages.taskpackagepool')),
            ],
        ),
        migrations.RunPython(migrations.RunPython.noop, prevent_pool_loss),
    ]
