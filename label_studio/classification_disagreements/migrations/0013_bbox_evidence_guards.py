from django.db import migrations


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    pin = 'classification_disagreements_bboxmediapin'
    proof = 'classification_disagreements_bboxsubmissionproof'
    guards = {
        'bbox_pin_scope': (pin, "EXISTS (SELECT 1 FROM task t WHERE t.id=NEW.task_id AND t.project_id=NEW.project_id)"),
        'bbox_proof_scope': (proof, "EXISTS (SELECT 1 FROM tasks_submission s JOIN tasks_taskassignment a ON a.id=s.assignment_id "
                             "JOIN classification_disagreements_bboxmediapin p ON p.id=NEW.media_id "
                             "WHERE s.id=NEW.submission_id AND s.assignment_id=NEW.assignment_id_frozen "
                             "AND s.revision=NEW.revision AND s.result_hash=NEW.result_hash "
                             "AND a.task_id=p.task_id AND a.project_id=p.project_id)"),
    }
    for name, (table, condition) in guards.items():
        if vendor == 'postgresql':
            schema_editor.execute(f"""CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NOT ({condition}) THEN RAISE EXCEPTION 'bbox evidence scope mismatch' USING ERRCODE = '23514'; END IF;
                RETURN NEW; END $$;
                CREATE TRIGGER {name} BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION {name}();""")
        elif vendor == 'sqlite':
            schema_editor.execute(f"""CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN NOT ({condition})
                BEGIN SELECT RAISE(ABORT, 'bbox evidence scope mismatch'); END;""")
    for name, table in (('bbox_pin_immutable', pin), ('bbox_proof_immutable', proof)):
        if vendor == 'postgresql':
            schema_editor.execute(f"""CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'bbox evidence is immutable' USING ERRCODE = '23514'; END $$;
                CREATE TRIGGER {name} BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION {name}();""")
        elif vendor == 'sqlite':
            schema_editor.execute(f"""CREATE TRIGGER {name} BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, 'bbox evidence is immutable'); END;""")


def uninstall(apps, schema_editor):
    tables = {
        'bbox_pin_scope': 'classification_disagreements_bboxmediapin',
        'bbox_proof_scope': 'classification_disagreements_bboxsubmissionproof',
        'bbox_pin_immutable': 'classification_disagreements_bboxmediapin',
        'bbox_proof_immutable': 'classification_disagreements_bboxsubmissionproof',
    }
    for name, table in tables.items():
        if schema_editor.connection.vendor == 'postgresql':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}; DROP FUNCTION IF EXISTS {name}();')
        elif schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name};')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0012_bboxmediapin_bboxsubmissionproof')]
    operations = [migrations.RunPython(install, uninstall)]
