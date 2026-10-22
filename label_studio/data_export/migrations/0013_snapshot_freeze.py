from django.db import migrations


def install(apps, editor):
    if editor.connection.vendor != "postgresql":
        return
    editor.execute("""
    CREATE FUNCTION data_export_snapshot_freeze() RETURNS trigger LANGUAGE plpgsql AS $$
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
        IF (to_jsonb(NEW) - ARRAY['state','token','started_at','error_code','manifest','manifest_sha256','content_digest'])
           IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['state','token','started_at','error_code','manifest','manifest_sha256','content_digest']) THEN
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
    CREATE TRIGGER snapshot_freeze BEFORE INSERT OR UPDATE OR DELETE ON data_export_releasesnapshot
    FOR EACH ROW EXECUTE FUNCTION data_export_snapshot_freeze();
    """)


def uninstall(apps, editor):
    if editor.connection.vendor != "postgresql":
        return
    # Hold the table fence across the check and DDL; rollback cannot erase retained evidence.
    editor.execute("LOCK TABLE data_export_releasesnapshot IN ACCESS EXCLUSIVE MODE")
    with editor.connection.cursor() as cursor:
        cursor.execute("SELECT EXISTS(SELECT 1 FROM data_export_releasesnapshot)")
        if cursor.fetchone()[0]:
            raise RuntimeError("Retained source snapshots forbid destructive rollback")
    editor.execute(
        "DROP TRIGGER snapshot_freeze ON data_export_releasesnapshot; DROP FUNCTION data_export_snapshot_freeze()"
    )


class Migration(migrations.Migration):
    dependencies = [("data_export", "0012_release_snapshot")]
    operations = [migrations.RunPython(install, uninstall)]
