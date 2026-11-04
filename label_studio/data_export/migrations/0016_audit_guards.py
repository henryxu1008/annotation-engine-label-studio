"""Immutable audit evidence, atomic account tombstones and parent-owned cleanup."""
from django.conf import settings
from django.db import migrations


def install(apps,editor):
    vendor=editor.connection.vendor
    if vendor not in ('postgresql','sqlite'):
        return
    sqlite=vendor=='sqlite'
    user=apps.get_model(*settings.AUTH_USER_MODEL.split('.'))._meta.db_table
    project=apps.get_model('projects','Project')._meta.db_table
    names=['AuditActorBinding','AuditPackage','AuditAttempt','AuditArtifact','AuditRetryReceipt','AuditAccessEvent']
    models={name:apps.get_model('data_export',name) for name in names}
    tables={name:m._meta.db_table for name,m in models.items()}
    b,p,a,f,r,e=[tables[n] for n in names]
    different='IS NOT' if sqlite else 'IS DISTINCT FROM'
    def null_safe(value):
        return f'COALESCE(({value}),'+('0' if sqlite else 'FALSE')+')'
    def json_value(column,key):
        return f"json_extract(NEW.{column},'$.{key}')" if sqlite else f"(NEW.{column}::jsonb->>'{key}')"
    def typed_number(column,key,expected):
        if sqlite:
            return f"json_type(NEW.{column},'$.{key}')='integer' AND {json_value(column,key)}={expected}"
        value=json_value(column,key)
        return f"jsonb_typeof(NEW.{column}::jsonb->'{key}')='number' AND {value} ~ '^[1-9][0-9]*$' AND {value}::numeric={expected}"
    def string_equal(column,key,expected):
        check=f"json_type(NEW.{column},'$.{key}')='text'" if sqlite else f"jsonb_typeof(NEW.{column}::jsonb->'{key}')='string'"
        return f'{check} AND {json_value(column,key)}={expected}'
    def hex_hash(column):
        return (f"length(NEW.{column})=64 AND NEW.{column} NOT GLOB '*[^0-9a-f]*'" if sqlite
                else f"NEW.{column} ~ '^[0-9a-f]{{64}}$'")
    def changed(model,exceptions=()):
        return ' OR '.join(f'NEW."{field.column}" {different} OLD."{field.column}"' for field in model._meta.fields if field.name not in exceptions)
    def trigger(table,name,event,condition,*,after=None):
        if sqlite:
            body=after if after else f"SELECT RAISE(ABORT,'Immutable audit protocol violation') WHERE {condition};"
            editor.execute(f'CREATE TRIGGER {name} {"AFTER" if after else "BEFORE"} {event} ON {table} BEGIN {body} END')
        else:
            body=after if after else f"IF {condition} THEN RAISE EXCEPTION 'Immutable audit protocol violation'; END IF;"
            ret='OLD' if event=='DELETE' else 'NEW'
            editor.execute(f'CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN {body} RETURN {ret}; END $$; '
                           f'CREATE TRIGGER {name} {"AFTER" if after else "BEFORE"} {event} ON {table} FOR EACH ROW EXECUTE FUNCTION {name}();')
    true='1' if sqlite else 'TRUE'
    false='0' if sqlite else 'FALSE'
    for name in names:
        table=tables[name]
        if name=='AuditActorBinding':
            valid=f'NEW.live_user_id IS NOT NULL AND NEW.tombstone={false} AND EXISTS(SELECT 1 FROM {user} WHERE id=NEW.live_user_id)'
            trigger(table,'audit_actor_initial','INSERT',f'NOT {null_safe(valid)}')
            immutable=changed(models[name],('live_user','tombstone'))
            allowed=(f'OLD.live_user_id IS NOT NULL AND OLD.tombstone={false} AND NEW.live_user_id IS NULL '
                     f'AND NEW.tombstone={true} AND NOT EXISTS(SELECT 1 FROM {user} WHERE id=OLD.live_user_id)')
            dynamic=changed(models[name],('id','instance_id','created_at'))
            trigger(table,'audit_actor_frozen','UPDATE',f'({immutable}) OR (({dynamic}) AND NOT {null_safe(allowed)})')
            trigger(table,'audit_actor_delete','DELETE',true)
        elif name=='AuditPackage':
            valid=(f"NEW.state='dispatch_pending' AND NEW.token IS NULL AND NEW.attempt_number=0 AND NEW.manifest IS NULL "
                   f"AND NEW.manifest_sha256='' AND NEW.error_code='' AND NEW.queued_at IS NULL AND NEW.started_at IS NULL "
                   f'AND EXISTS(SELECT 1 FROM {b} WHERE id=NEW.creator_id AND instance_id=NEW.instance_id '
                   f'AND live_user_id IS NOT NULL AND tombstone={false}) '
                   f'AND NEW.organization_id_frozen=(SELECT organization_id FROM {project} WHERE id=NEW.project_id) '
                   'AND length(NEW.input_sha256)=64 AND length(NEW.projection_sha256)=64 AND length(NEW.request_hash)=64')
            for column in ('input_sha256','projection_sha256','request_hash','owner_hash'):
                valid+=' AND ('+hex_hash(column)+')'
            # Native JSON/type predicates protect birth scope as well as frozen
            # updates. Readers still verify canonical bytes and their SHA.
            for column in ('input_bundle','projection','access_policy','summary_record'):
                valid+=f' AND json_valid(NEW.{column})' if sqlite else f" AND jsonb_typeof(NEW.{column}::jsonb)='object'"
            package_id="substr(NEW.id,1,8)||'-'||substr(NEW.id,9,4)||'-'||substr(NEW.id,13,4)||'-'||substr(NEW.id,17,4)||'-'||substr(NEW.id,21,12)" if sqlite else 'NEW.id::text'
            generation="substr(NEW.creator_id,1,8)||'-'||substr(NEW.creator_id,9,4)||'-'||substr(NEW.creator_id,13,4)||'-'||substr(NEW.creator_id,17,4)||'-'||substr(NEW.creator_id,21,12)" if sqlite else 'NEW.creator_id::text'
            for column,key,expected in (
                ('input_bundle','schema',"'review-audit-source/v1'"),('projection','schema',"'review-audit-source/v1'"),
                ('projection','profile',"'review-audit-redacted/v1'"),('projection','effect',"'supervisory_only'"),
                ('input_bundle','package_id',package_id),('projection','package_id',package_id),
                ('input_bundle','creator_generation',generation),('input_bundle','owner_hash','NEW.owner_hash'),
                ('input_bundle','request_hash','NEW.request_hash'),('input_bundle','projection_sha256','NEW.projection_sha256')):
                valid+=' AND ('+string_equal(column,key,expected)+')'
            valid+=' AND ('+typed_number('input_bundle','project_id','NEW.project_id')+')'
            valid+=' AND ('+typed_number('input_bundle','organization_id','NEW.organization_id_frozen')+')'
            valid+=' AND length(NEW.creator_principal) BETWEEN 1 AND 512 AND length(NEW.creator_source) BETWEEN 1 AND 128'
            trigger(table,'audit_package_initial','INSERT',f'NOT {null_safe(valid)}')
            mutable=('state','token','attempt_number','queued_at','started_at','error_code','manifest','manifest_sha256')
            frozen=changed(models[name],mutable)
            sealed=changed(models[name],('state','error_code'))
            artifact=(f'EXISTS(SELECT 1 FROM {f} z JOIN {a} t ON t.id=z.attempt_id WHERE z.package_id=NEW.id '
                      f"AND t.package_id=NEW.id AND t.id=NEW.token AND t.number=NEW.attempt_number AND t.state='ready')")
            queued=(f"OLD.state='dispatch_pending' AND NEW.token IS NOT NULL AND NEW.attempt_number=OLD.attempt_number+1 "
                    f'AND NEW.queued_at IS NOT NULL AND EXISTS(SELECT 1 FROM {a} WHERE id=NEW.token '
                    f"AND package_id=NEW.id AND number=NEW.attempt_number AND state='queued')")
            tokenchange=f'NEW.token {different} OLD.token OR NEW.attempt_number {different} OLD.attempt_number'
            transitions=(f"(NEW.state=OLD.state OR (NEW.state='queued' AND ({queued})) "
                f"OR (OLD.state='queued' AND NEW.state='running' AND NEW.started_at IS NOT NULL) "
                f"OR (OLD.state IN ('queued','running') AND NEW.state IN ('blocked_auth','blocked_runtime','failed_retryable','failed_final')) "
                f"OR (OLD.state IN ('queued','running','failed_retryable','blocked_auth','blocked_runtime') AND NEW.state='dispatch_pending') "
                f"OR (OLD.state='running' AND NEW.state='ready') OR (OLD.state='ready' AND NEW.state='quarantined'))")
            guard=(f'({frozen}) OR NOT {null_safe(transitions)} '
                   f"OR (OLD.state IN ('ready','quarantined') AND (({sealed}) OR (OLD.state='quarantined' AND NEW.state<>'quarantined'))) "
                   f"OR (({tokenchange}) AND NOT (NEW.state='queued' AND ({queued}))) "
                   f"OR (NEW.state='ready' AND (NEW.manifest IS NULL OR length(NEW.manifest_sha256)<>64 OR NOT ({artifact}))) "
                   f"OR (NEW.state NOT IN ('ready','quarantined') AND (NEW.manifest IS NOT NULL OR NEW.manifest_sha256<>''))")
            guard+=(f' OR (NEW.queued_at {different} OLD.queued_at AND NOT (NEW.state=\'queued\' AND ({queued}))) '
                    f'OR (NEW.started_at {different} OLD.started_at AND NOT '
                    f"((OLD.state='queued' AND NEW.state='running') OR (NEW.state='queued' AND ({queued}) AND NEW.started_at IS NULL)))")
            # Frozen migration protocol; never import mutable product constants.
            ERROR_CODES = frozenset('AUDIT_'+name for name in (
                'SOURCE_INTEGRITY_INVALID','SQL_BUDGET','TOO_LARGE','SOURCE_CHANGED','ROOT_UNAVAILABLE','UNSUPPORTED_INPUT',
                'INPUT_SCOPE_UNPROVABLE','PROJECTION_INVALID','RUNTIME_CHANGED','INPUT_INVALID','SCRATCH_LIMIT','FILE_INVALID',
                'PRIVATE_STORAGE_REQUIRED','INVALID','POLICY_INVALID','MODE_FORBIDDEN','VIEWER_CHANGED','INVALID_REQUEST',
                'NOT_FOUND','NOT_READY','INVALID_CURSOR','FORBIDDEN','BACKGROUND_UNAVAILABLE','COLLECTION_UNAVAILABLE',
                'AUTHORITY_CHANGED','POLICY_CHANGED','CREATOR_REVOKED','INVALID_KEY','QUEUE_UNAVAILABLE','IDEMPOTENCY_CONFLICT',
                'ACTIVE_LIMIT','PREVIEW_CHANGED','NOT_RETRYABLE','IO_FAILED','GENERATION_FAILED','REQUEST_REJECTED'))
            allowed_codes=','.join("'"+value+"'" for value in sorted(ERROR_CODES|{''}))
            guard+=f' OR NEW.error_code NOT IN ({allowed_codes})'
            trigger(table,'audit_package_frozen','UPDATE',guard)
            trigger(table,'audit_package_delete','DELETE',f'EXISTS(SELECT 1 FROM {project} WHERE id=OLD.project_id)')
        elif name=='AuditAttempt':
            valid=(f"NEW.state='queued' AND NEW.finished_at IS NULL AND EXISTS(SELECT 1 FROM {p} WHERE id=NEW.package_id "
                   f"AND state='dispatch_pending' AND NEW.number=attempt_number+1)")
            trigger(table,'audit_attempt_initial','INSERT',f'NOT {null_safe(valid)}')
            fixed=changed(models[name],('state','finished_at'))
            current=f'EXISTS(SELECT 1 FROM {p} WHERE id=NEW.package_id AND token=NEW.id AND attempt_number=NEW.number)'
            states="NEW.state IN ('queued','running','ready','blocked_auth','blocked_runtime','failed_retryable','failed_final','superseded')"
            allowed=f"OLD.state IN ('queued','running') AND ({current}) AND ({states})"
            trigger(table,'audit_attempt_frozen','UPDATE',f'({fixed}) OR (({changed(models[name])}) AND NOT {null_safe(allowed)})')
            trigger(table,'audit_attempt_delete','DELETE',f'EXISTS(SELECT 1 FROM {p} WHERE id=OLD.package_id)')
        else:
            if name=='AuditArtifact':
                valid=(f'EXISTS(SELECT 1 FROM {p} x JOIN {a} t ON t.package_id=x.id WHERE x.id=NEW.package_id '
                       f"AND x.state='running' AND x.token=NEW.attempt_id AND t.id=NEW.attempt_id AND t.state='running') "
                       'AND NEW.size_bytes>0 AND NEW.size_bytes<=16777216 AND length(NEW.sha256)=64')
                trigger(table,'audit_artifact_initial','INSERT',f'NOT {null_safe(valid)}')
            trigger(table,'audit_'+name.lower()+'_frozen','UPDATE',changed(models[name]))
            parent=f'EXISTS(SELECT 1 FROM {project} WHERE id=OLD.project_id)' if name=='AuditAccessEvent' else f'EXISTS(SELECT 1 FROM {p} WHERE id=OLD.package_id)'
            trigger(table,'audit_'+name.lower()+'_delete','DELETE',parent)
    trigger(user,'audit_user_tombstone','DELETE','',after=f'UPDATE {b} SET live_user_id=NULL,tombstone={true} WHERE live_user_id=OLD.id;')
    # Package first: deferred child FKs remain valid when all rows are cleaned in
    # this same parent deletion statement. Actor generations are never removed.
    trigger(project,'audit_project_cleanup','DELETE','',after=(
        f'DELETE FROM {p} WHERE project_id=OLD.id; DELETE FROM {e} WHERE project_id=OLD.id; '
        f'DELETE FROM {r} WHERE NOT EXISTS(SELECT 1 FROM {p} WHERE id={r}.package_id); '
        f'DELETE FROM {f} WHERE NOT EXISTS(SELECT 1 FROM {p} WHERE id={f}.package_id); '
        f'DELETE FROM {a} WHERE NOT EXISTS(SELECT 1 FROM {p} WHERE id={a}.package_id);'))


def uninstall(apps,editor):
    tables=[apps.get_model('data_export',n)._meta.db_table for n in (
        'AuditActorBinding','AuditPackage','AuditAttempt','AuditArtifact','AuditRetryReceipt','AuditAccessEvent')]
    if editor.connection.vendor not in ('postgresql','sqlite'):
        return
    if editor.connection.vendor=='postgresql':
        editor.execute('LOCK TABLE '+','.join(tables)+' IN ACCESS EXCLUSIVE MODE')
    with editor.connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f'SELECT EXISTS(SELECT 1 FROM {table})')
            if cursor.fetchone()[0]:
                raise RuntimeError('Retained audit namespace forbids destructive rollback')
        if editor.connection.vendor=='sqlite':
            cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'audit_%'")
            names=[r[0] for r in cursor.fetchall()]
            for name in names:
                editor.execute(f'DROP TRIGGER {name}')
        else:
            cursor.execute("SELECT tgname, tgrelid::regclass::text FROM pg_trigger WHERE NOT tgisinternal AND tgname LIKE 'audit_%'")
            for name,table in cursor.fetchall():
                editor.execute(f'DROP TRIGGER {name} ON {table}; DROP FUNCTION {name}();')


class Migration(migrations.Migration):
    dependencies=[('data_export','0015_review_audit'),('review_batches','0010_concern_followup_guards'),
                  ('users','0014_usersessionrevocationboundary_sessionrevocationevent'),
                  ('organizations','0006_alter_organizationmember_deleted_at')]
    operations=[migrations.RunPython(install,uninstall)]
