from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindMixCompletion')._meta.db_table)
    evidence = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindMixEvidence')._meta.db_table)
    decision = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindMixDecision')._meta.db_table)
    columns = ('decision_id_frozen, project_id_frozen, source_id_frozen, source_task_id_frozen, '
               'source_assignee_id_frozen, chosen_id_frozen')
    selected = ('id, project_id, source_id_frozen, source_task_id_frozen, '
                'source_assignee_id_frozen, chosen_id_frozen')
    schema_editor.execute(f'INSERT INTO {evidence} ({columns}) SELECT {selected} FROM {decision} WHERE selected = TRUE')
    if vendor == 'postgresql':
        schema_editor.execute('''CREATE FUNCTION blind_mix_completion_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'blind mix completion is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_mix_completion_immutable BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION blind_mix_completion_immutable()''')
        schema_editor.execute(f'''CREATE TRIGGER blind_mix_evidence_immutable BEFORE UPDATE OR DELETE ON {evidence}
            FOR EACH ROW EXECUTE FUNCTION blind_mix_completion_immutable()''')
        schema_editor.execute(f'''CREATE FUNCTION blind_mix_capture_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN IF NEW.selected THEN INSERT INTO {evidence} ({columns}) VALUES
            (NEW.id, NEW.project_id, NEW.source_id_frozen, NEW.source_task_id_frozen,
             NEW.source_assignee_id_frozen, NEW.chosen_id_frozen); END IF; RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_mix_capture_evidence AFTER INSERT ON {decision}
            FOR EACH ROW EXECUTE FUNCTION blind_mix_capture_evidence()''')
    else:
        for suffix, operation in (('update', 'UPDATE'), ('delete', 'DELETE')):
            schema_editor.execute(f'''CREATE TRIGGER blind_mix_completion_{suffix} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT, 'blind mix completion is immutable'); END''')
            schema_editor.execute(f'''CREATE TRIGGER blind_mix_evidence_{suffix} BEFORE {operation} ON {evidence}
                BEGIN SELECT RAISE(ABORT, 'blind mix evidence is immutable'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_mix_capture_evidence AFTER INSERT ON {decision}
          WHEN NEW.selected = TRUE BEGIN INSERT INTO {evidence} ({columns}) VALUES
            (NEW.id, NEW.project_id, NEW.source_id_frozen, NEW.source_task_id_frozen,
             NEW.source_assignee_id_frozen, NEW.chosen_id_frozen); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindMixCompletion')._meta.db_table)
        evidence = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindMixEvidence')._meta.db_table)
        decision = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindMixDecision')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_mix_capture_evidence ON {decision}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_mix_capture_evidence()')
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_mix_evidence_immutable ON {evidence}')
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_mix_completion_immutable ON {table}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_mix_completion_immutable()')
    elif vendor == 'sqlite':
        schema_editor.execute('DROP TRIGGER IF EXISTS blind_mix_capture_evidence')
        for suffix in ('update', 'delete'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_mix_completion_{suffix}')
            schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_mix_evidence_{suffix}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0034_blind_task_handle')]
    operations = [
        migrations.CreateModel(name='BlindMixEvidence', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('decision_id_frozen', models.PositiveBigIntegerField(unique=True)),
            ('project_id_frozen', models.PositiveBigIntegerField()),
            ('source_id_frozen', models.PositiveBigIntegerField()),
            ('source_task_id_frozen', models.PositiveBigIntegerField()),
            ('source_assignee_id_frozen', models.PositiveBigIntegerField()),
            ('chosen_id_frozen', models.PositiveBigIntegerField(unique=True)),
        ]),
        migrations.CreateModel(name='BlindMixCompletion', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('decision_id_frozen', models.PositiveBigIntegerField(unique=True)),
            ('project_id_frozen', models.PositiveBigIntegerField()),
            ('source_id_frozen', models.PositiveBigIntegerField()),
            ('source_version_after', models.PositiveBigIntegerField()),
            ('chosen_id_frozen', models.PositiveBigIntegerField(unique=True)),
            ('submission_id_frozen', models.PositiveBigIntegerField(unique=True)),
        ]),
        migrations.RunPython(install, uninstall),
    ]
