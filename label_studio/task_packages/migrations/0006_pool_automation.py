import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def preserve_pools(apps, schema_editor):
    if apps.get_model('task_packages', 'TaskPackagePool').objects.using(schema_editor.connection.alias).exists():
        raise RuntimeError('Cannot reverse package automation while pool configuration or claims exist.')


class Migration(migrations.Migration):
    dependencies = [('task_packages', '0005_overdue_notice'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(model_name='taskpackagepool', name='automatic', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='taskpackagepool', name='automation_generation', field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name='taskpackagepool', name='automation_actor', field=models.ForeignKey(
            null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
        migrations.RunPython(migrations.RunPython.noop, preserve_pools),
    ]
