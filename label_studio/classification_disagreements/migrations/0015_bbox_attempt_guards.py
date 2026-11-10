from django.db import migrations

TABLE = 'classification_disagreements_bboxmediaattempt'
PIN = 'classification_disagreements_bboxmediapin'
FIELDS = ('token', 'task_id_frozen', 'project_id_frozen', 'actor_id_frozen', 'storage_binding', 'expires_at', 'created_at')


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    insert = ("NEW.status='active' AND NEW.task_id=NEW.task_id_frozen AND NEW.project_id=NEW.project_id_frozen "
              "AND EXISTS (SELECT 1 FROM task t WHERE t.id=NEW.task_id AND t.project_id=NEW.project_id)")
    publish = (f"EXISTS (SELECT 1 FROM {PIN} p WHERE p.token=NEW.token AND p.task_id=NEW.task_id "
               "AND p.project_id=NEW.project_id AND p.storage_binding=NEW.storage_binding)")
    pin_query = (f"SELECT 1 FROM {TABLE} a WHERE a.token=NEW.token AND a.status='active' "
                 "AND a.task_id=NEW.task_id AND a.project_id=NEW.project_id "
                 "AND a.storage_binding=NEW.storage_binding")
    pin_query += (" AND a.expires_at > CURRENT_TIMESTAMP" if vendor == 'postgresql'
                  else " AND julianday(a.expires_at) > julianday('now')")
    pin_attempt = f"EXISTS ({pin_query}{' FOR UPDATE' if vendor == 'postgresql' else ''})"
    if vendor == 'postgresql':
        old, new = (', '.join(f'{prefix}.{field}' for field in FIELDS) for prefix in ('OLD', 'NEW'))
        guard = (f'ROW({old}) IS DISTINCT FROM ROW({new}) '
                 "OR (NEW.task_id IS NOT NULL AND NEW.task_id IS DISTINCT FROM OLD.task_id) "
                 "OR (NEW.project_id IS NOT NULL AND NEW.project_id IS DISTINCT FROM OLD.project_id) "
                 "OR (OLD.status <> 'active' AND NEW.status <> OLD.status) "
                 "OR (OLD.status='active' AND NEW.status NOT IN ('active','published','abandoned')) "
                 f"OR (OLD.status='active' AND NEW.status='published' AND NOT ({publish}))")
        schema_editor.execute(f"""CREATE FUNCTION bbox_attempt_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NOT ({insert}) THEN RAISE EXCEPTION 'bbox attempt scope mismatch' USING ERRCODE='23514'; END IF;
            RETURN NEW; END $$;
            CREATE TRIGGER bbox_attempt_insert BEFORE INSERT ON {TABLE} FOR EACH ROW EXECUTE FUNCTION bbox_attempt_insert();
            CREATE FUNCTION bbox_attempt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF {guard} THEN RAISE EXCEPTION 'invalid bbox attempt transition' USING ERRCODE='23514'; END IF;
            RETURN NEW; END $$;
            CREATE TRIGGER bbox_attempt_guard BEFORE UPDATE ON {TABLE} FOR EACH ROW EXECUTE FUNCTION bbox_attempt_guard();
            CREATE FUNCTION bbox_pin_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NOT ({pin_attempt}) THEN RAISE EXCEPTION 'bbox pin attempt mismatch' USING ERRCODE='23514'; END IF;
            RETURN NEW; END $$;
            CREATE TRIGGER bbox_pin_attempt BEFORE INSERT ON {PIN} FOR EACH ROW EXECUTE FUNCTION bbox_pin_attempt();""")
    elif vendor == 'sqlite':
        changed = ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in FIELDS)
        guard = (f"{changed} OR (NEW.task_id IS NOT NULL AND NEW.task_id IS NOT OLD.task_id) "
                 "OR (NEW.project_id IS NOT NULL AND NEW.project_id IS NOT OLD.project_id) "
                 "OR (OLD.status <> 'active' AND NEW.status <> OLD.status) "
                 "OR (OLD.status='active' AND NEW.status NOT IN ('active','published','abandoned')) "
                 f"OR (OLD.status='active' AND NEW.status='published' AND NOT ({publish}))")
        schema_editor.execute(f"""CREATE TRIGGER bbox_attempt_insert BEFORE INSERT ON {TABLE} WHEN NOT ({insert})
            BEGIN SELECT RAISE(ABORT, 'bbox attempt scope mismatch'); END;""")
        schema_editor.execute(f"""CREATE TRIGGER bbox_attempt_guard BEFORE UPDATE ON {TABLE} WHEN {guard}
            BEGIN SELECT RAISE(ABORT, 'invalid bbox attempt transition'); END;""")
        schema_editor.execute(f"""CREATE TRIGGER bbox_pin_attempt BEFORE INSERT ON {PIN} WHEN NOT ({pin_attempt})
            BEGIN SELECT RAISE(ABORT, 'bbox pin attempt mismatch'); END;""")


def uninstall(apps, schema_editor):
    for name, table in (('bbox_attempt_insert', TABLE), ('bbox_attempt_guard', TABLE), ('bbox_pin_attempt', PIN)):
        if schema_editor.connection.vendor == 'postgresql':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}; DROP FUNCTION IF EXISTS {name}();')
        elif schema_editor.connection.vendor == 'sqlite':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name};')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0014_bboxmediaattempt')]
    operations = [migrations.RunPython(install, uninstall)]
