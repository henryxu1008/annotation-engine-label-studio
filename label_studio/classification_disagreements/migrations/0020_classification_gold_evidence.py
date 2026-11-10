import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


TABLE = 'classification_disagreements_classificationgoldevidence'
FIELDS = ('id', 'project_id', 'actor_id_frozen', 'principal', 'principal_source', 'created_at', 'kind',
          'idempotency_key', 'request_hash', 'manifest', 'manifest_hash')


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        old, new = (', '.join(f'{prefix}.{field}' for field in FIELDS) for prefix in ('OLD', 'NEW'))
        schema_editor.execute(f"""CREATE FUNCTION classification_gold_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN IF ROW({old}) IS DISTINCT FROM ROW({new}) OR
            (NEW.created_by_id IS NOT NULL AND NEW.created_by_id IS DISTINCT FROM OLD.created_by_id)
            THEN RAISE EXCEPTION 'gold evidence is immutable' USING ERRCODE='23514'; END IF;
          RETURN NEW; END $$; CREATE TRIGGER classification_gold_immutable BEFORE UPDATE ON {TABLE}
          FOR EACH ROW EXECUTE FUNCTION classification_gold_immutable();""")
    elif vendor == 'sqlite':
        changed = ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in FIELDS)
        schema_editor.execute(f"""CREATE TRIGGER classification_gold_immutable BEFORE UPDATE ON {TABLE}
          WHEN {changed} OR (NEW.created_by_id IS NOT NULL AND NEW.created_by_id IS NOT OLD.created_by_id)
          BEGIN SELECT RAISE(ABORT, 'gold evidence is immutable'); END;""")


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(f'DROP TRIGGER IF EXISTS classification_gold_immutable ON {TABLE}; '
                              'DROP FUNCTION IF EXISTS classification_gold_immutable();')
    elif schema_editor.connection.vendor == 'sqlite':
        schema_editor.execute('DROP TRIGGER IF EXISTS classification_gold_immutable;')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0019_bbox_unique_source'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='ClassificationGoldEvidence', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('actor_id_frozen', models.PositiveBigIntegerField()),
            ('principal', models.CharField(max_length=512)),
            ('principal_source', models.CharField(max_length=128)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('kind', models.CharField(max_length=12)),
            ('idempotency_key', models.UUIDField()),
            ('request_hash', models.CharField(max_length=64)),
            ('manifest', models.JSONField()),
            ('manifest_hash', models.CharField(max_length=64)),
            ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                             to=settings.AUTH_USER_MODEL)),
            ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                          related_name='classification_gold_evidence', to='projects.project')),
        ], options={'constraints': [
            models.UniqueConstraint(fields=('project', 'idempotency_key'), name='classification_gold_project_key_unique'),
            models.CheckConstraint(condition=models.Q(kind__in=['reference', 'score']), name='classification_gold_kind_valid'),
        ]}),
        migrations.RunPython(install, uninstall),
    ]
