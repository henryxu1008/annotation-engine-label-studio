"""Serialize direct mixed-history inserts with fenced participant reads."""

from django.db import migrations


def install(apps, schema_editor):
    # SQLite fenced reads already reserve the database writer, which serializes
    # all inserts. PostgreSQL needs an explicit conflicting row lock.
    if schema_editor.connection.vendor != 'postgresql':
        return
    q = schema_editor.quote_name
    def table(name):
        return q(apps.get_model('classification_disagreements', name)._meta.db_table)

    epoch = table('BlindCaptureEpoch')
    decision = table('BlindMixDecision')
    legacy = table('BlindMixEvidence')
    schema_editor.execute(f'''CREATE FUNCTION blind_mix_insert_capture_fence() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        PERFORM id FROM {epoch} WHERE id = 1 FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'capture epoch is unavailable'; END IF;
        RETURN NULL;
      END $$''')
    for name, target in (('blind_mix_decision_insert_fence', decision),
                         ('blind_mix_legacy_insert_fence', legacy)):
        schema_editor.execute(f'''CREATE TRIGGER {name} BEFORE INSERT ON {target}
          FOR EACH STATEMENT EXECUTE FUNCTION blind_mix_insert_capture_fence()''')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    q = schema_editor.quote_name
    for name, model in (('blind_mix_decision_insert_fence', 'BlindMixDecision'),
                        ('blind_mix_legacy_insert_fence', 'BlindMixEvidence')):
        target = q(apps.get_model('classification_disagreements', model)._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER {name} ON {target}')
    schema_editor.execute('DROP FUNCTION blind_mix_insert_capture_fence()')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0072_blind_bound_legacy_history')]
    operations = [migrations.RunPython(install, uninstall)]
