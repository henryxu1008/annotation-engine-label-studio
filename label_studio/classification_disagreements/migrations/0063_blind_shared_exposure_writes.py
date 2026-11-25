"""Use the retained exposure ledgers instead of purpose-specific write work."""

from django.db import migrations


def _tables(apps, schema_editor):
    q = schema_editor.quote_name
    names = (
        'BlindReadIdentityState',
        'BlindReadBinding',
        'BlindCaptureCertificate',
        'BlindTextLengthExposure',
        'BlindReferenceTextLength',
        'BlindAssignmentExposure',
        'BlindEligibilityProof',
    )
    return [q(apps.get_model('classification_disagreements', name)._meta.db_table) for name in names]


def install(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    state, binding, cert, text, lengths, assigned, proof = _tables(apps, schema_editor)
    # Wait for all in-flight events before changing which write path maintains
    # the state. Exposure ledgers remain immutable and the live read is fenced.
    schema_editor.execute(
        f'LOCK TABLE {binding}, {cert}, {text}, {lengths}, {assigned}, {proof}, {state} ' 'IN SHARE ROW EXCLUSIVE MODE'
    )
    schema_editor.execute(f'DROP TRIGGER blind_read_text_revoke ON {text}')
    schema_editor.execute(f'DROP TRIGGER blind_read_assignment_exposure_revoke ON {assigned}')


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    state, binding, cert, text, lengths, assigned, proof = _tables(apps, schema_editor)
    schema_editor.execute(
        f'LOCK TABLE {binding}, {cert}, {text}, {lengths}, {assigned}, {proof}, {state} ' 'IN SHARE ROW EXCLUSIVE MODE'
    )
    # Earlier readers rely on the per-binding revocation bit. Reconcile every
    # event written while these triggers were absent before restoring them.
    schema_editor.execute(f'DROP TRIGGER blind_read_identity_state_guard ON {state}')
    schema_editor.execute(
        f'''UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND EXISTS (
      SELECT 1 FROM {binding} b JOIN {cert} c ON c.account_id = b.actor_id_frozen
      WHERE b.assignment_id_frozen = {state}.assignment_id AND b.purpose = 'quality' AND (
        EXISTS (SELECT 1 FROM {text} x WHERE x.account_id = c.account_id AND x.generation = c.generation
          AND x.assignment_id <> b.assignment_id_frozen AND (x.byte_length = -1 OR EXISTS
            (SELECT 1 FROM {lengths} l WHERE l.reference_id = b.reference_id_frozen AND l.byte_length = x.byte_length)))
        OR EXISTS (SELECT 1 FROM {assigned} x JOIN {proof} p ON p.assignment_id = b.assignment_id_frozen
          AND p.manifest_hash = b.proof_hash WHERE x.account_id = c.account_id AND x.generation = c.generation
          AND p.manifest->>'rule' = 'local-unseen-source/v2'
          AND p.manifest->>'source_task_id' = CAST(x.task_id AS TEXT))))'''
    )
    schema_editor.execute(
        f'''CREATE TRIGGER blind_read_identity_state_guard BEFORE INSERT OR UPDATE OR DELETE ON {state}
      FOR EACH ROW EXECUTE FUNCTION blind_read_identity_state_guard()'''
    )
    for name, table in (('blind_read_text_revoke', text), ('blind_read_assignment_exposure_revoke', assigned)):
        schema_editor.execute(
            f'CREATE TRIGGER {name} AFTER INSERT ON {table} '
            'FOR EACH ROW EXECUTE FUNCTION blind_read_evidence_revoke()'
        )


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0062_blind_privilege_read_state')]
    operations = [migrations.RunPython(install, uninstall)]
