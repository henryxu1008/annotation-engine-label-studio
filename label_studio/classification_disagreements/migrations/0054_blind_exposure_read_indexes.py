from django.db import migrations, models

from classification_disagreements.migration_operations import AddIndexOnline


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('classification_disagreements', '0053_blind_completion_state_fence')]
    operations = [
        AddIndexOnline('blindprojectfileexposure', models.Index(
            fields=['account_id', 'generation'], name='blind_file_read_scope_idx',
        )),
    ]
