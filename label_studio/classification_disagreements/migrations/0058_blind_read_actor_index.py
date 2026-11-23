from django.db import migrations, models

from classification_disagreements.migration_operations import AddIndexOnline


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('classification_disagreements', '0057_blind_read_evidence_revocation')]
    operations = [AddIndexOnline('blindreadbinding', models.Index(
        fields=['actor_id_frozen', 'purpose'], name='blind_read_actor_scope_idx',
    ))]
