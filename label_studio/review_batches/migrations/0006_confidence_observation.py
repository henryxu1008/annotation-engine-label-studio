import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

TABLE = 'review_batches_reviewconfidenceobservation'
TRIGGER = 'review_confidence_observation_frozen'
FROZEN = (
    'id', 'project_id', 'instance_id', 'producer_namespace', 'profile_id', 'profile_digest', 'event_uuid',
    'submission_id_frozen', 'actor_id_frozen', 'creator_principal', 'creator_source', 'request_hash',
    'proof', 'receipt', 'fact_hash', 'receipt_hash', 'created_at',
)
NULLABLE = ('submission_id', 'created_by_id')


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        old, new = (', '.join(f'{prefix}.{f}' for f in FROZEN) for prefix in ('OLD', 'NEW'))
        guard = f'ROW({old}) IS DISTINCT FROM ROW({new})'
        guard += ' OR ' + ' OR '.join(
            f'(NEW.{f} IS NOT NULL AND NEW.{f} IS DISTINCT FROM OLD.{f})' for f in NULLABLE
        )
        schema_editor.execute(f'''
            CREATE FUNCTION {TRIGGER}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF {guard} THEN RAISE EXCEPTION 'Confidence observation is immutable'; END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER {TRIGGER} BEFORE UPDATE ON {TABLE}
            FOR EACH ROW EXECUTE FUNCTION {TRIGGER}();
        ''')
    elif vendor == 'sqlite':
        guard = ' OR '.join(f'OLD.{f} IS NOT NEW.{f}' for f in FROZEN)
        guard += ' OR ' + ' OR '.join(
            f'(NEW.{f} IS NOT NULL AND NEW.{f} IS NOT OLD.{f})' for f in NULLABLE
        )
        schema_editor.execute(
            f'CREATE TRIGGER {TRIGGER} BEFORE UPDATE ON {TABLE} WHEN {guard} '
            "BEGIN SELECT RAISE(ABORT, 'Confidence observation is immutable'); END;"
        )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(f'DROP TRIGGER IF EXISTS {TRIGGER} ON {TABLE}; DROP FUNCTION IF EXISTS {TRIGGER}();')
    elif schema_editor.connection.vendor == 'sqlite':
        schema_editor.execute(f'DROP TRIGGER IF EXISTS {TRIGGER};')


class Migration(migrations.Migration):
    dependencies = [
        ('review_batches', '0005_field_arbitration_handoff'),
        ('tasks', '0062_submission_reviewdecision'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name='ReviewConfidenceObservation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('instance_id', models.UUIDField()),
                ('producer_namespace', models.UUIDField()),
                ('profile_id', models.UUIDField()),
                ('profile_digest', models.CharField(max_length=64)),
                ('event_uuid', models.UUIDField()),
                ('submission_id_frozen', models.PositiveBigIntegerField()),
                ('actor_id_frozen', models.PositiveBigIntegerField()),
                ('creator_principal', models.CharField(max_length=512)),
                ('creator_source', models.CharField(max_length=128)),
                ('request_hash', models.CharField(max_length=64)),
                ('proof', models.JSONField()),
                ('receipt', models.JSONField()),
                ('fact_hash', models.CharField(max_length=64)),
                ('receipt_hash', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='projects.project')),
                ('submission', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='tasks.submission')),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={'constraints': [
                models.UniqueConstraint(fields=('project', 'instance_id', 'producer_namespace', 'event_uuid'), name='confidence_event_unique'),
                models.UniqueConstraint(fields=('project', 'instance_id', 'profile_id', 'profile_digest', 'submission_id_frozen'), name='confidence_subject_unique'),
            ]},
        ),
        migrations.RunPython(install, uninstall),
    ]
