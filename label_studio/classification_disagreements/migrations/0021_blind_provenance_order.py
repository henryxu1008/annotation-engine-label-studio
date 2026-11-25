from django.conf import settings
from django.db import migrations, models


FENCE = 'classification_disagreements_blindprovenancefence'
EVENT = 'classification_disagreements_blindprovenanceevent'
GOLD = 'classification_disagreements_classificationgoldevidence'


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    user = apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table
    q = schema_editor.quote_name
    fence, event, gold, user = map(q, (FENCE, EVENT, GOLD, user))
    schema_editor.execute(f'INSERT INTO {fence} (id, sequence) VALUES (1, 0)')

    if vendor == 'postgresql':
        schema_editor.execute(f'''
            CREATE FUNCTION blind_provenance_capture() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE ticket bigint;
            BEGIN
                INSERT INTO {fence} (id, sequence)
                    VALUES (1, (SELECT COALESCE(MAX(sequence), 0) FROM {event})) ON CONFLICT (id) DO NOTHING;
                UPDATE {fence} SET sequence = sequence + 1 WHERE id = 1 RETURNING sequence INTO ticket;
                IF ticket IS NULL THEN RAISE EXCEPTION 'blind provenance fence missing'; END IF;
                INSERT INTO {event} (kind, subject_id, sequence) VALUES (TG_ARGV[0], NEW.id, ticket);
                RETURN NEW;
            END $$;
        ''')
        schema_editor.execute(f'''CREATE TRIGGER blind_provenance_account AFTER INSERT ON {user}
            FOR EACH ROW EXECUTE FUNCTION blind_provenance_capture('account')''')
        schema_editor.execute(f'''CREATE TRIGGER blind_provenance_reference AFTER INSERT ON {gold}
            FOR EACH ROW WHEN (NEW.kind = 'reference') EXECUTE FUNCTION blind_provenance_capture('reference')''')
        schema_editor.execute('''CREATE FUNCTION blind_provenance_no_update() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'blind provenance event is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_provenance_immutable BEFORE UPDATE ON {event}
            FOR EACH ROW EXECUTE FUNCTION blind_provenance_no_update()''')
        schema_editor.execute('''CREATE FUNCTION blind_provenance_fence_forward() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.id <> OLD.id OR NEW.sequence < OLD.sequence THEN
                RAISE EXCEPTION 'blind provenance fence cannot move backwards'; END IF;
            RETURN NEW; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_provenance_fence_forward BEFORE UPDATE ON {fence}
            FOR EACH ROW EXECUTE FUNCTION blind_provenance_fence_forward()''')
    else:
        for name, table, kind, condition in (
            ('blind_provenance_account', user, 'account', ''),
            ('blind_provenance_reference', gold, 'reference', "WHEN NEW.kind = 'reference'"),
        ):
            schema_editor.execute(f'''CREATE TRIGGER {name} AFTER INSERT ON {table} {condition}
                BEGIN
                    INSERT INTO {fence} (id, sequence)
                        VALUES (1, (SELECT COALESCE(MAX(sequence), 0) FROM {event})) ON CONFLICT (id) DO NOTHING;
                    UPDATE {fence} SET sequence = sequence + 1 WHERE id = 1;
                    INSERT INTO {event} (kind, subject_id, sequence)
                        VALUES ('{kind}', NEW.id, (SELECT sequence FROM {fence} WHERE id = 1));
                END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_provenance_immutable BEFORE UPDATE ON {event}
            BEGIN SELECT RAISE(ABORT, 'blind provenance event is immutable'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_provenance_fence_forward BEFORE UPDATE ON {fence}
            WHEN NEW.id IS NOT OLD.id OR NEW.sequence < OLD.sequence
            BEGIN SELECT RAISE(ABORT, 'blind provenance fence cannot move backwards'); END''')

    # Existing references are conservatively ordered at migration time. Old
    # accounts are intentionally not backfilled: their creation is unproven.
    # Trigger creation holds source-table DDL locks until this transaction ends.
    schema_editor.execute(f'''INSERT INTO {event} (kind, subject_id, sequence)
        SELECT 'reference', id, ROW_NUMBER() OVER (ORDER BY id) FROM {gold} WHERE kind = 'reference' ''')
    schema_editor.execute(f'UPDATE {fence} SET sequence = (SELECT COUNT(*) FROM {event}) WHERE id = 1')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    user = schema_editor.quote_name(apps.get_model(settings.AUTH_USER_MODEL)._meta.db_table)
    if vendor == 'postgresql':
        for name, table in (('blind_provenance_account', user), ('blind_provenance_reference', GOLD),
                            ('blind_provenance_immutable', EVENT), ('blind_provenance_fence_forward', FENCE)):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_provenance_capture()')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_provenance_no_update()')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_provenance_fence_forward()')
    else:
        for name in ('blind_provenance_account', 'blind_provenance_reference', 'blind_provenance_immutable',
                     'blind_provenance_fence_forward'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0020_classification_gold_evidence'),
                    ('users', '0014_usersessionrevocationboundary_sessionrevocationevent'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='BlindProvenanceFence', fields=[
            ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
            ('sequence', models.PositiveBigIntegerField(default=0)),
        ]),
        migrations.CreateModel(name='BlindProvenanceEvent', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('kind', models.CharField(max_length=12)),
            ('subject_id', models.PositiveBigIntegerField()),
            ('sequence', models.PositiveBigIntegerField(unique=True)),
        ], options={'constraints': [
            models.UniqueConstraint(fields=('kind', 'subject_id'), name='blind_provenance_subject_unique'),
            models.CheckConstraint(condition=models.Q(kind__in=['account', 'reference']),
                                   name='blind_provenance_kind_valid'),
        ]}),
        migrations.RunPython(install, uninstall),
    ]
