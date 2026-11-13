import django.db.models.deletion
from django.db import migrations, models


def install(apps, schema_editor):
    table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindMixDecision')._meta.db_table)
    immutable = ('id', 'project_id', 'source_id_frozen', 'source_task_id_frozen', 'source_assignee_id_frozen',
                 'chosen_id_frozen', 'reference_id_frozen', 'predecessor_id_frozen',
                 'key', 'request_hash', 'actor_id', 'ratio_basis_points', 'selected')
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        fields_old = ', '.join(f'OLD.{name}' for name in immutable)
        fields_new = ', '.join(f'NEW.{name}' for name in immutable)
        schema_editor.execute(f'''CREATE FUNCTION blind_mix_decision_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN IF ROW({fields_old}) IS DISTINCT FROM ROW({fields_new})
             OR (NEW.source_assignment_id IS DISTINCT FROM OLD.source_assignment_id AND NEW.source_assignment_id IS NOT NULL)
             OR (NEW.chosen_assignment_id IS DISTINCT FROM OLD.chosen_assignment_id AND NEW.chosen_assignment_id IS NOT NULL)
            THEN RAISE EXCEPTION 'blind mix decision is immutable'; END IF; RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_mix_decision_immutable BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION blind_mix_decision_immutable()''')
    elif vendor == 'sqlite':
        changed = ' OR '.join(f'OLD.{name} IS NOT NEW.{name}' for name in immutable)
        schema_editor.execute(f'''CREATE TRIGGER blind_mix_decision_immutable BEFORE UPDATE ON {table}
          WHEN {changed}
             OR (NEW.source_assignment_id IS NOT OLD.source_assignment_id AND NEW.source_assignment_id IS NOT NULL)
             OR (NEW.chosen_assignment_id IS NOT OLD.chosen_assignment_id AND NEW.chosen_assignment_id IS NOT NULL)
          BEGIN SELECT RAISE(ABORT, 'blind mix decision is immutable'); END''')


def uninstall(apps, schema_editor):
    table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindMixDecision')._meta.db_table)
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_mix_decision_immutable ON {table}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_mix_decision_immutable()')
    elif schema_editor.connection.vendor == 'sqlite':
        schema_editor.execute('DROP TRIGGER IF EXISTS blind_mix_decision_immutable')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0030_blind_storage_capture_fence')]
    operations = [
        migrations.CreateModel(name='BlindMixDecision', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('source_id_frozen', models.PositiveBigIntegerField()),
            ('source_task_id_frozen', models.PositiveBigIntegerField()),
            ('source_assignee_id_frozen', models.PositiveBigIntegerField()),
            ('chosen_id_frozen', models.PositiveBigIntegerField()),
            ('reference_id_frozen', models.PositiveBigIntegerField(null=True)),
            ('predecessor_id_frozen', models.PositiveBigIntegerField(null=True, unique=True)),
            ('key', models.UUIDField()),
            ('request_hash', models.CharField(max_length=64)),
            ('actor_id', models.PositiveBigIntegerField()),
            ('ratio_basis_points', models.PositiveSmallIntegerField()),
            ('selected', models.BooleanField()),
            ('source_assignment', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                    related_name='+', to='tasks.taskassignment')),
            ('chosen_assignment', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                    related_name='+', to='tasks.taskassignment')),
            ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='projects.project')),
        ], options={'constraints': [
            models.UniqueConstraint(fields=['project', 'key'], name='blind_mix_project_key_unique'),
            models.UniqueConstraint(fields=['project', 'source_id_frozen'], name='blind_mix_source_unique'),
            models.UniqueConstraint(fields=['chosen_id_frozen'], name='blind_mix_chosen_unique'),
            models.CheckConstraint(condition=models.Q(ratio_basis_points__lte=10000), name='blind_mix_ratio_valid'),
            models.CheckConstraint(condition=(models.Q(selected=False, reference_id_frozen__isnull=True)
                                             | models.Q(selected=True, reference_id_frozen__isnull=False)),
                                   name='blind_mix_reference_valid'),
        ]}),
        migrations.RunPython(install, uninstall),
    ]
