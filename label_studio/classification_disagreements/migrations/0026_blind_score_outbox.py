import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0025_blind_gold_score'),
                    ('tasks', '0063_annotationdraft_revision')]
    operations = [
        migrations.CreateModel(name='BlindScoreOutbox', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('token', models.UUIDField(default=uuid.uuid4)),
            ('state', models.CharField(default='pending', max_length=16)),
            ('queued_at', models.DateTimeField(null=True)),
            ('updated_at', models.DateTimeField(auto_now=True)),
            ('error_code', models.CharField(blank=True, default='', max_length=64)),
            ('submission', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                                related_name='blind_score_outbox', to='tasks.submission')),
        ], options={'constraints': [
            models.CheckConstraint(condition=models.Q(state__in=['pending', 'queued', 'scored', 'ignored', 'invalid']),
                                   name='blind_score_outbox_state_valid'),
        ]}),
    ]
