from django.db import migrations, models

from classification_disagreements.migration_operations import AddIndexOnline


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('classification_disagreements', '0054_blind_exposure_read_indexes')]
    operations = [AddIndexOnline('blindtextlengthexposure', models.Index(
        fields=['account_id', 'generation', 'byte_length', 'assignment_id'], name='blind_text_read_scope_idx',
    ))]
