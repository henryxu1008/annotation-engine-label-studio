from importlib import import_module

from django.db import migrations, models
from django.utils import timezone


def install(apps, editor):
    if editor.connection.vendor == "postgresql":
        editor.execute("""
        CREATE OR REPLACE FUNCTION data_export_snapshot_freeze() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Source snapshot history cannot be deleted';
          ELSIF TG_OP = 'INSERT' THEN
            IF NEW.project_id IS DISTINCT FROM (SELECT project_id FROM data_export_approveddatasetrelease WHERE id=NEW.release_id) THEN
              RAISE EXCEPTION 'Snapshot project must match its source receipt';
            END IF;
            IF NEW.state <> 'dispatch_pending' OR NEW.manifest IS NOT NULL
               OR NEW.manifest_sha256 <> '' OR NEW.content_digest <> '' THEN
              RAISE EXCEPTION 'Snapshot must start uncaptured';
            END IF;
          ELSE
            IF (to_jsonb(NEW) - ARRAY['state','token','queued_at','started_at','error_code','manifest','manifest_sha256','content_digest'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['state','token','queued_at','started_at','error_code','manifest','manifest_sha256','content_digest']) THEN
              RAISE EXCEPTION 'Snapshot source and authority are immutable';
            END IF;
            IF OLD.state IN ('ready','quarantined') THEN
              IF (to_jsonb(NEW) - ARRAY['state','error_code']) IS DISTINCT FROM
                 (to_jsonb(OLD) - ARRAY['state','error_code']) OR
                 (OLD.state = 'ready' AND NEW.state NOT IN ('ready','quarantined')) OR
                 (OLD.state = 'quarantined' AND NEW.state <> 'quarantined') THEN
                RAISE EXCEPTION 'Frozen snapshot cannot be rewritten';
              END IF;
            ELSIF NEW.state = 'ready' AND (OLD.state <> 'running' OR NEW.manifest IS NULL
                  OR length(NEW.manifest_sha256) <> 64 OR length(NEW.content_digest) <> 64) THEN
              RAISE EXCEPTION 'Incomplete snapshot cannot be finalized';
            END IF;
          END IF;
          RETURN NEW;
        END $$;
        """)
    # Historical enqueue time is unknown: grant existing queued claims a fresh
    # recovery window without changing their token, source or frozen READY data.
    editor.execute(
        "UPDATE data_export_releasesnapshot SET queued_at=%s WHERE state='queued' AND queued_at IS NULL",
        [timezone.now()],
    )


def uninstall(apps, editor):
    original = import_module("data_export.migrations.0013_snapshot_freeze")
    # Reuse the retained-evidence rollback fence, then restore the prior guard.
    original.uninstall(apps, editor)
    original.install(apps, editor)


class Migration(migrations.Migration):
    dependencies = [("data_export", "0013_snapshot_freeze")]
    operations = [
        migrations.AddField(model_name="releasesnapshot", name="queued_at", field=models.DateTimeField(null=True)),
        migrations.RunPython(install, uninstall),
    ]
