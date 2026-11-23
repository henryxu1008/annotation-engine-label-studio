from django.db import migrations, models

from classification_disagreements.migration_operations import AddIndexOnline


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('classification_disagreements', '0058_blind_read_actor_index')]
    operations = [
        AddIndexOnline(
            'blindreadbinding',
            models.Index(
                fields=['reference_id_frozen', 'purpose'],
                name='blind_read_ref_scope_idx',
            ),
        )
    ]
