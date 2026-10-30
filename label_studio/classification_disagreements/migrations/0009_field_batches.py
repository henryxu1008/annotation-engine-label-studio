import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

FIELDS = ('id', 'project_id', 'creator_principal', 'actor_id_frozen', 'created_at',
          'name', 'idempotency_key', 'request_hash', 'manifest', 'manifest_hash')


def install(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        old, new = ', '.join('OLD.' + f for f in FIELDS), ', '.join('NEW.' + f for f in FIELDS)
        schema_editor.execute(f"""
            CREATE FUNCTION field_batch_frozen() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF ROW({old}) IS DISTINCT FROM ROW({new}) OR
                   (NEW.created_by_id IS NOT NULL AND NEW.created_by_id IS DISTINCT FROM OLD.created_by_id) THEN
                    RAISE EXCEPTION 'Field batch is immutable';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER field_batch_frozen BEFORE UPDATE ON classification_disagreements_fieldbatch
            FOR EACH ROW EXECUTE FUNCTION field_batch_frozen();
        """)
    elif schema_editor.connection.vendor == 'sqlite':
        changed = ' OR '.join(f'OLD.{f} IS NOT NEW.{f}' for f in FIELDS)
        schema_editor.execute(f"""
            CREATE TRIGGER field_batch_frozen BEFORE UPDATE ON classification_disagreements_fieldbatch
            WHEN {changed} OR (NEW.created_by_id IS NOT NULL AND NEW.created_by_id IS NOT OLD.created_by_id)
            BEGIN SELECT RAISE(ABORT, 'Field batch is immutable'); END;
        """)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP TRIGGER IF EXISTS field_batch_frozen ON classification_disagreements_fieldbatch;')
        schema_editor.execute('DROP FUNCTION IF EXISTS field_batch_frozen();')
    elif schema_editor.connection.vendor == 'sqlite':
        schema_editor.execute('DROP TRIGGER IF EXISTS field_batch_frozen;')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0008_ner_arbitration_guards'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='FieldBatch', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('creator_principal', models.CharField(max_length=512)),
            ('actor_id_frozen', models.PositiveBigIntegerField()),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('name', models.CharField(max_length=160)),
            ('idempotency_key', models.UUIDField()),
            ('request_hash', models.CharField(max_length=64)),
            ('manifest', models.JSONField()),
            ('manifest_hash', models.CharField(max_length=64)),
            ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='field_batches', to='projects.project')),
        ], options={'constraints': [models.UniqueConstraint(fields=('project', 'idempotency_key'), name='field_batch_project_key_unique')]}),
        migrations.RunPython(install, uninstall),
    ]
