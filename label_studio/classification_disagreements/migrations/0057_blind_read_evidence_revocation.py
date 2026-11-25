"""Retain selected quality evidence revocations in the shared read state.

Live evidence checks remain authoritative: this migration does not project all
eligibility, exposure and source conditions and does not enable mixed work.
"""

from django.db import migrations


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    state = q(apps.get_model('classification_disagreements', 'BlindReadIdentityState')._meta.db_table)
    binding = q(apps.get_model('classification_disagreements', 'BlindReadBinding')._meta.db_table)
    assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
    certificate = q(apps.get_model('classification_disagreements', 'BlindCaptureCertificate')._meta.db_table)
    history = q(apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table)
    event = q(apps.get_model('classification_disagreements', 'BlindProvenanceEvent')._meta.db_table)
    reference = q(apps.get_model('classification_disagreements', 'ClassificationGoldEvidence')._meta.db_table)
    files = q(apps.get_model('classification_disagreements', 'BlindProjectFileExposure')._meta.db_table)
    labels = q(apps.get_model('classification_disagreements', 'BlindLabelExposure')._meta.db_table)
    text = q(apps.get_model('classification_disagreements', 'BlindTextLengthExposure')._meta.db_table)
    lengths = q(apps.get_model('classification_disagreements', 'BlindReferenceTextLength')._meta.db_table)
    assigned = q(apps.get_model('classification_disagreements', 'BlindAssignmentExposure')._meta.db_table)
    proof = q(apps.get_model('classification_disagreements', 'BlindEligibilityProof')._meta.db_table)
    epoch = q(apps.get_model('classification_disagreements', 'BlindCaptureEpoch')._meta.db_table)
    proof_rule = "p.manifest->>'rule'" if vendor == 'postgresql' else "json_extract(p.manifest, '$.rule')"
    proof_source = (
        "p.manifest->>'source_task_id'"
        if vendor == 'postgresql'
        else "CAST(json_extract(p.manifest, '$.source_task_id') AS TEXT)"
    )
    # Lock all source tables before installing the snapshot and triggers so a
    # concurrent write cannot fall between backfill and trigger installation.
    if vendor == 'postgresql':
        schema_editor.execute(
            f'LOCK TABLE {assignment}, {binding}, {certificate}, {history}, {event}, {reference}, {files}, {labels}, {text}, {lengths}, {assigned}, {proof}, {state} IN SHARE ROW EXCLUSIVE MODE'
        )
        schema_editor.execute(f'DROP TRIGGER blind_read_identity_state_guard ON {state}')
    schema_editor.execute(
        f'''UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND EXISTS (
      SELECT 1 FROM {binding} b WHERE b.assignment_id_frozen = {state}.assignment_id AND b.purpose = 'quality'
      AND (NOT EXISTS (SELECT 1 FROM {certificate} c WHERE c.account_id = b.actor_id_frozen)
        OR NOT EXISTS (SELECT 1 FROM {history} h WHERE h.account_id = b.actor_id_frozen AND NOT h.elevated AND h.had_assignment)
        OR NOT EXISTS (SELECT 1 FROM {event} e WHERE e.kind = 'account' AND e.subject_id = b.actor_id_frozen)
        OR NOT EXISTS (SELECT 1 FROM {event} e WHERE e.kind = 'reference' AND e.subject_id = b.reference_id_frozen)
        OR NOT EXISTS (SELECT 1 FROM {reference} r WHERE r.id = b.reference_id_frozen
          AND r.project_id = b.project_id_frozen AND r.kind = 'reference' AND r.manifest_hash = b.reference_hash)
        OR EXISTS (SELECT 1 FROM {certificate} c JOIN {files} f ON f.account_id = c.account_id
          AND f.generation = c.generation WHERE c.account_id = b.actor_id_frozen)
        OR EXISTS (SELECT 1 FROM {certificate} c JOIN {labels} l ON l.account_id = c.account_id
          AND l.generation = c.generation WHERE c.account_id = b.actor_id_frozen)
        OR EXISTS (SELECT 1 FROM {certificate} c JOIN {text} x ON x.account_id = c.account_id
          AND x.generation = c.generation WHERE c.account_id = b.actor_id_frozen
          AND x.assignment_id <> b.assignment_id_frozen AND (x.byte_length = -1 OR EXISTS
            (SELECT 1 FROM {lengths} rl WHERE rl.reference_id = b.reference_id_frozen AND rl.byte_length = x.byte_length)))
        OR EXISTS (SELECT 1 FROM {certificate} c JOIN {assigned} x ON x.account_id = c.account_id
          AND x.generation = c.generation JOIN {proof} p ON p.assignment_id = b.assignment_id_frozen
          AND p.manifest_hash = b.proof_hash WHERE c.account_id = b.actor_id_frozen
          AND {proof_rule} = 'local-unseen-source/v2' AND {proof_source} = CAST(x.task_id AS TEXT))))'''
    )
    if vendor == 'postgresql':
        schema_editor.execute(
            f'''CREATE TRIGGER blind_read_identity_state_guard BEFORE INSERT OR UPDATE OR DELETE ON {state}
          FOR EACH ROW EXECUTE FUNCTION blind_read_identity_state_guard()'''
        )
        schema_editor.execute(
            f'''CREATE FUNCTION blind_read_evidence_revoke() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF TG_TABLE_NAME IN ('{apps.get_model('classification_disagreements', 'BlindCaptureCertificate')._meta.db_table}',
                                 '{apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table}') THEN
              PERFORM id FROM {epoch} WHERE id = 1 FOR UPDATE;
              UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
                (SELECT assignment_id_frozen FROM {binding} WHERE purpose = 'quality' AND actor_id_frozen = OLD.account_id);
              RETURN OLD;
            ELSIF TG_TABLE_NAME = '{apps.get_model('classification_disagreements', 'BlindProvenanceEvent')._meta.db_table}' THEN
              PERFORM id FROM {epoch} WHERE id = 1 FOR UPDATE;
              UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
                (SELECT assignment_id_frozen FROM {binding} WHERE purpose = 'quality'
                  AND ((OLD.kind = 'account' AND actor_id_frozen = OLD.subject_id)
                    OR (OLD.kind = 'reference' AND reference_id_frozen = OLD.subject_id)));
              RETURN OLD;
            ELSIF TG_TABLE_NAME = '{apps.get_model('classification_disagreements', 'ClassificationGoldEvidence')._meta.db_table}' THEN
              IF OLD.kind = 'reference' THEN
                PERFORM id FROM {epoch} WHERE id = 1 FOR UPDATE;
                UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
                  (SELECT assignment_id_frozen FROM {binding} WHERE purpose = 'quality' AND reference_id_frozen = OLD.id);
              END IF;
              RETURN OLD;
            ELSIF TG_TABLE_NAME = '{apps.get_model('classification_disagreements', 'BlindReferenceTextLength')._meta.db_table}' THEN
              UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
                (SELECT assignment_id_frozen FROM {binding} WHERE purpose = 'quality' AND reference_id_frozen = NEW.reference_id);
              RETURN NEW;
            ELSIF TG_TABLE_NAME = '{apps.get_model('classification_disagreements', 'BlindTextLengthExposure')._meta.db_table}' THEN
              UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
                (SELECT b.assignment_id_frozen FROM {binding} b JOIN {certificate} c ON c.account_id = b.actor_id_frozen
                  WHERE b.purpose = 'quality' AND c.account_id = NEW.account_id AND c.generation = NEW.generation
                    AND b.assignment_id_frozen <> NEW.assignment_id AND (NEW.byte_length = -1 OR EXISTS
                      (SELECT 1 FROM {lengths} rl WHERE rl.reference_id = b.reference_id_frozen AND rl.byte_length = NEW.byte_length)));
              RETURN NEW;
            ELSIF TG_TABLE_NAME = '{apps.get_model('classification_disagreements', 'BlindAssignmentExposure')._meta.db_table}' THEN
              UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
                (SELECT b.assignment_id_frozen FROM {binding} b JOIN {certificate} c ON c.account_id = b.actor_id_frozen
                  JOIN {proof} p ON p.assignment_id = b.assignment_id_frozen AND p.manifest_hash = b.proof_hash
                  WHERE b.purpose = 'quality' AND c.account_id = NEW.account_id AND c.generation = NEW.generation
                    AND {proof_rule} = 'local-unseen-source/v2' AND {proof_source} = CAST(NEW.task_id AS TEXT));
              RETURN NEW;
            END IF;
            UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
              (SELECT b.assignment_id_frozen FROM {binding} b JOIN {certificate} c ON c.account_id = b.actor_id_frozen
                WHERE b.purpose = 'quality' AND c.account_id = NEW.account_id AND c.generation = NEW.generation);
            RETURN NEW;
          END $$'''
        )
        for name, table, operation in (
            ('blind_read_certificate_revoke', certificate, 'BEFORE DELETE'),
            ('blind_read_history_revoke', history, 'BEFORE DELETE'),
            ('blind_read_event_revoke', event, 'BEFORE DELETE'),
            ('blind_read_reference_revoke', reference, 'BEFORE DELETE'),
            ('blind_read_file_revoke', files, 'AFTER INSERT'),
            ('blind_read_label_revoke', labels, 'AFTER INSERT'),
            ('blind_read_text_revoke', text, 'AFTER INSERT'),
            ('blind_read_length_revoke', lengths, 'AFTER INSERT'),
            ('blind_read_assignment_exposure_revoke', assigned, 'AFTER INSERT'),
        ):
            schema_editor.execute(
                f'CREATE TRIGGER {name} {operation} ON {table} FOR EACH ROW EXECUTE FUNCTION blind_read_evidence_revoke()'
            )
    else:
        schema_editor.execute(
            f'''CREATE TRIGGER blind_read_certificate_revoke BEFORE DELETE ON {certificate}
          BEGIN UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
            (SELECT assignment_id_frozen FROM {binding} WHERE purpose = 'quality' AND actor_id_frozen = OLD.account_id); END'''
        )
        schema_editor.execute(
            f'''CREATE TRIGGER blind_read_history_revoke BEFORE DELETE ON {history}
          BEGIN UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
            (SELECT assignment_id_frozen FROM {binding} WHERE purpose = 'quality' AND actor_id_frozen = OLD.account_id); END'''
        )
        schema_editor.execute(
            f'''CREATE TRIGGER blind_read_event_revoke BEFORE DELETE ON {event}
          BEGIN UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
            (SELECT assignment_id_frozen FROM {binding} WHERE purpose = 'quality'
              AND ((OLD.kind = 'account' AND actor_id_frozen = OLD.subject_id)
                OR (OLD.kind = 'reference' AND reference_id_frozen = OLD.subject_id))); END'''
        )
        schema_editor.execute(
            f'''CREATE TRIGGER blind_read_reference_revoke BEFORE DELETE ON {reference} WHEN OLD.kind = 'reference'
          BEGIN UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
            (SELECT assignment_id_frozen FROM {binding} WHERE purpose = 'quality' AND reference_id_frozen = OLD.id); END'''
        )
        for name, table in (('blind_read_file_revoke', files), ('blind_read_label_revoke', labels)):
            schema_editor.execute(
                f'''CREATE TRIGGER {name} AFTER INSERT ON {table}
              BEGIN UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
                (SELECT b.assignment_id_frozen FROM {binding} b JOIN {certificate} c ON c.account_id = b.actor_id_frozen
                  WHERE b.purpose = 'quality' AND c.account_id = NEW.account_id AND c.generation = NEW.generation); END'''
            )
        schema_editor.execute(
            f'''CREATE TRIGGER blind_read_length_revoke AFTER INSERT ON {lengths}
          BEGIN UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
            (SELECT assignment_id_frozen FROM {binding} WHERE purpose = 'quality' AND reference_id_frozen = NEW.reference_id); END'''
        )
        schema_editor.execute(
            f'''CREATE TRIGGER blind_read_text_revoke AFTER INSERT ON {text}
          BEGIN UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
            (SELECT b.assignment_id_frozen FROM {binding} b JOIN {certificate} c ON c.account_id = b.actor_id_frozen
              WHERE b.purpose = 'quality' AND c.account_id = NEW.account_id AND c.generation = NEW.generation
                AND b.assignment_id_frozen <> NEW.assignment_id AND (NEW.byte_length = -1 OR EXISTS
                  (SELECT 1 FROM {lengths} rl WHERE rl.reference_id = b.reference_id_frozen AND rl.byte_length = NEW.byte_length))); END'''
        )
        schema_editor.execute(
            f'''CREATE TRIGGER blind_read_assignment_exposure_revoke AFTER INSERT ON {assigned}
          BEGIN UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND assignment_id IN
            (SELECT b.assignment_id_frozen FROM {binding} b JOIN {certificate} c ON c.account_id = b.actor_id_frozen
              JOIN {proof} p ON p.assignment_id = b.assignment_id_frozen AND p.manifest_hash = b.proof_hash
              WHERE b.purpose = 'quality' AND c.account_id = NEW.account_id AND c.generation = NEW.generation
                AND {proof_rule} = 'local-unseen-source/v2' AND {proof_source} = CAST(NEW.task_id AS TEXT)); END'''
        )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    models = (
        ('blind_read_certificate_revoke', 'BlindCaptureCertificate'),
        ('blind_read_history_revoke', 'BlindPrivilegeHistory'),
        ('blind_read_event_revoke', 'BlindProvenanceEvent'),
        ('blind_read_reference_revoke', 'ClassificationGoldEvidence'),
        ('blind_read_file_revoke', 'BlindProjectFileExposure'),
        ('blind_read_label_revoke', 'BlindLabelExposure'),
        ('blind_read_text_revoke', 'BlindTextLengthExposure'),
        ('blind_read_length_revoke', 'BlindReferenceTextLength'),
        ('blind_read_assignment_exposure_revoke', 'BlindAssignmentExposure'),
    )
    for name, model in models:
        if schema_editor.connection.vendor == 'postgresql':
            table = q(apps.get_model('classification_disagreements', model)._meta.db_table)
            schema_editor.execute(f'DROP TRIGGER {name} ON {table}')
        else:
            schema_editor.execute(f'DROP TRIGGER {name}')
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('DROP FUNCTION blind_read_evidence_revoke()')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0056_blind_read_identity_state')]
    operations = [migrations.RunPython(install, uninstall)]
