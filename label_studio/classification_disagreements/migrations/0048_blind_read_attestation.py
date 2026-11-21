from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    table = q(apps.get_model('classification_disagreements', 'BlindReadAttestation')._meta.db_table)
    if vendor == 'postgresql':
        schema_editor.execute('''CREATE FUNCTION blind_read_attestation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'blind read attestation is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_read_attestation_immutable BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION blind_read_attestation_immutable()''')
    else:
        for operation in ('UPDATE', 'DELETE'):
            schema_editor.execute(f'''CREATE TRIGGER blind_read_attestation_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT, 'blind read attestation is immutable'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindReadAttestation')._meta.db_table)
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_read_attestation_immutable ON {table}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_read_attestation_immutable()')
    elif vendor == 'sqlite':
        for operation in ('update', 'delete'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_read_attestation_{operation}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0047_blind_ordinary_read_binding')]
    operations = [
        migrations.CreateModel(name='BlindReadAttestation', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('assignment_id_frozen', models.PositiveBigIntegerField(unique=True)),
            ('signed_payload', models.TextField()),
        ]),
        migrations.RunPython(install, uninstall),
    ]
