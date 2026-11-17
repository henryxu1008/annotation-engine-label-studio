import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def refuse_lossy_reverse(apps, schema_editor):
    alias = schema_editor.connection.alias
    project = apps.get_model('projects', 'Project')
    event = apps.get_model('projects', 'ProjectLifecycleEvent')
    if event.objects.using(alias).exists() or project.objects.using(alias).exclude(
        lifecycle_state='legacy', lifecycle_revision=0
    ).exists():
        raise RuntimeError('Cannot reverse lifecycle migration while lifecycle state or events exist.')


def add_lifecycle_columns(apps, schema_editor):
    # SQLite's normal AddField rebuilds project and invalidates existing review
    # triggers which reference it. Add these constant-default columns in place.
    schema_editor.execute("ALTER TABLE project ADD COLUMN lifecycle_state varchar(16) NOT NULL DEFAULT 'legacy'")
    schema_editor.execute('ALTER TABLE project ADD COLUMN lifecycle_revision bigint NOT NULL DEFAULT 0 CHECK (lifecycle_revision >= 0)')


def remove_lifecycle_columns(apps, schema_editor):
    schema_editor.execute('ALTER TABLE project DROP COLUMN lifecycle_revision')
    schema_editor.execute('ALTER TABLE project DROP COLUMN lifecycle_state')


def add_lifecycle_constraint(apps, schema_editor):
    values = "'legacy','draft','active','paused','completed','archived'"
    if schema_editor.connection.vendor == 'sqlite':
        for action in ('INSERT', 'UPDATE OF lifecycle_state'):
            name = 'project_lifecycle_' + ('insert' if action == 'INSERT' else 'update')
            schema_editor.execute(
                f'CREATE TRIGGER {name} BEFORE {action} ON project '
                f'WHEN NEW.lifecycle_state NOT IN ({values}) '
                "BEGIN SELECT RAISE(ABORT, 'invalid project lifecycle state'); END"
            )
    else:
        schema_editor.execute(
            f'ALTER TABLE project ADD CONSTRAINT valid_project_lifecycle_state '
            f'CHECK (lifecycle_state IN ({values}))'
        )


def remove_lifecycle_constraint(apps, schema_editor):
    if schema_editor.connection.vendor == 'sqlite':
        schema_editor.execute('DROP TRIGGER IF EXISTS project_lifecycle_insert')
        schema_editor.execute('DROP TRIGGER IF EXISTS project_lifecycle_update')
    else:
        schema_editor.execute('ALTER TABLE project DROP CONSTRAINT valid_project_lifecycle_state')


class Migration(migrations.Migration):
    dependencies = [
        ('projects', '0035_projectmember_role_and_unique'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(add_lifecycle_columns, remove_lifecycle_columns)],
            state_operations=[
                migrations.AddField(model_name='project', name='lifecycle_state', field=models.CharField(default='legacy', max_length=16)),
                migrations.AddField(model_name='project', name='lifecycle_revision', field=models.PositiveBigIntegerField(default=0)),
            ],
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(add_lifecycle_constraint, remove_lifecycle_constraint)],
            state_operations=[migrations.AddConstraint(
                model_name='project',
                constraint=models.CheckConstraint(
                    condition=models.Q(lifecycle_state__in=['legacy', 'draft', 'active', 'paused', 'completed', 'archived']),
                    name='valid_project_lifecycle_state',
                ),
            )],
        ),
        migrations.CreateModel(
            name='ProjectLifecycleEvent',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('previous_state', models.CharField(max_length=16)),
                ('next_state', models.CharField(max_length=16)),
                ('revision', models.PositiveBigIntegerField()),
                ('actor_id_snapshot', models.BigIntegerField()),
                ('reason', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='lifecycle_events', to='projects.project')),
            ],
            options={'constraints': [models.UniqueConstraint(fields=('project', 'revision'), name='unique_project_lifecycle_revision')]},
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_lossy_reverse),
    ]
