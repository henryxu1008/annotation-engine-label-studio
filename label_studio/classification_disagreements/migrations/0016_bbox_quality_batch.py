import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def install(apps, schema_editor):
    table = 'classification_disagreements_bboxqualitybatch'
    fields = ('id', 'project_id', 'creator_principal', 'creator_source', 'actor_id_frozen',
              'created_at', 'name', 'idempotency_key', 'request_hash', 'manifest', 'manifest_hash')
    if schema_editor.connection.vendor == 'postgresql':
        old, new = (', '.join(f'{prefix}.{field}' for field in fields) for prefix in ('OLD', 'NEW'))
        schema_editor.execute(f"""CREATE FUNCTION bbox_quality_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF ROW({old}) IS DISTINCT FROM ROW({new}) OR
              (NEW.created_by_id IS NOT NULL AND NEW.created_by_id IS DISTINCT FROM OLD.created_by_id)
              THEN RAISE EXCEPTION 'bbox quality evidence is immutable' USING ERRCODE='23514'; END IF;
              RETURN NEW; END $$;
            CREATE TRIGGER bbox_quality_immutable BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION bbox_quality_immutable();""")
    elif schema_editor.connection.vendor == 'sqlite':
        changed = ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in fields)
        schema_editor.execute(f"""CREATE TRIGGER bbox_quality_immutable BEFORE UPDATE ON {table}
            WHEN {changed} OR (NEW.created_by_id IS NOT NULL AND NEW.created_by_id IS NOT OLD.created_by_id)
            BEGIN SELECT RAISE(ABORT, 'bbox quality evidence is immutable'); END;""")


def uninstall(apps, schema_editor):
    table = 'classification_disagreements_bboxqualitybatch'
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(f'DROP TRIGGER IF EXISTS bbox_quality_immutable ON {table}; DROP FUNCTION IF EXISTS bbox_quality_immutable();')
    elif schema_editor.connection.vendor == 'sqlite':
        schema_editor.execute('DROP TRIGGER IF EXISTS bbox_quality_immutable;')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0015_bbox_attempt_guards'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(
            name='BboxQualityBatch',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('creator_principal', models.CharField(max_length=512)),
                ('creator_source', models.CharField(max_length=128)),
                ('actor_id_frozen', models.PositiveBigIntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('name', models.CharField(max_length=160)),
                ('idempotency_key', models.UUIDField()),
                ('request_hash', models.CharField(max_length=64)),
                ('manifest', models.JSONField()),
                ('manifest_hash', models.CharField(max_length=64)),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='bbox_quality_batches', to='projects.project')),
            ],
            options={'constraints': [models.UniqueConstraint(fields=('project', 'idempotency_key'),
                                                            name='bbox_quality_project_key_unique')]},
        ),
        migrations.RunPython(install, uninstall),
    ]
