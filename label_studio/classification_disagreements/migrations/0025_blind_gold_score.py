import django.db.models.deletion
from django.db import migrations, models


def install(apps, schema_editor):
    table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindGoldScore')._meta.db_table)
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('''CREATE FUNCTION blind_gold_score_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'blind gold score is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_gold_score_immutable BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION blind_gold_score_immutable()''')
    elif schema_editor.connection.vendor == 'sqlite':
        schema_editor.execute(f'''CREATE TRIGGER blind_gold_score_immutable BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, 'blind gold score is immutable'); END''')


def uninstall(apps, schema_editor):
    table = schema_editor.quote_name(apps.get_model('classification_disagreements', 'BlindGoldScore')._meta.db_table)
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(f'DROP TRIGGER IF EXISTS blind_gold_score_immutable ON {table}')
        schema_editor.execute('DROP FUNCTION IF EXISTS blind_gold_score_immutable()')
    elif schema_editor.connection.vendor == 'sqlite':
        schema_editor.execute('DROP TRIGGER IF EXISTS blind_gold_score_immutable')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0024_blind_quality_assignment'),
                    ('tasks', '0063_annotationdraft_revision')]
    operations = [
        migrations.CreateModel(name='BlindGoldScore', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('manifest', models.JSONField()),
            ('manifest_hash', models.CharField(max_length=64)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('submission', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                                related_name='blind_gold_score', to='tasks.submission')),
        ]),
        migrations.RunPython(install, uninstall),
    ]
