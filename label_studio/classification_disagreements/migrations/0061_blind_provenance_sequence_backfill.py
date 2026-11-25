"""Retain mismatched historical provenance sequences before a future read fast path.

Live evidence checks remain authoritative. Missing proofs are left to the live
path, including legacy unsigned work. Revocation is monotone and is not undone
when this data migration is reversed.
"""

from django.db import migrations


def reconcile(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    state = q(apps.get_model('classification_disagreements', 'BlindReadIdentityState')._meta.db_table)
    binding = q(apps.get_model('classification_disagreements', 'BlindReadBinding')._meta.db_table)
    proof = q(apps.get_model('classification_disagreements', 'BlindEligibilityProof')._meta.db_table)
    event = q(apps.get_model('classification_disagreements', 'BlindProvenanceEvent')._meta.db_table)
    if vendor == 'postgresql':
        schema_editor.execute(f'LOCK TABLE {binding}, {proof}, {event}, {state} IN SHARE ROW EXCLUSIVE MODE')
        schema_editor.execute(f'DROP TRIGGER blind_read_identity_state_guard ON {state}')
        account_match = "p.manifest->'account_birth_sequence' = to_jsonb(e.sequence)"
        reference_match = "p.manifest->'reference_sequence' = to_jsonb(e.sequence)"
    else:
        account_match = (
            "json_type(p.manifest, '$.account_birth_sequence') = 'integer' AND "
            "json_extract(p.manifest, '$.account_birth_sequence') = e.sequence"
        )
        reference_match = (
            "json_type(p.manifest, '$.reference_sequence') = 'integer' AND "
            "json_extract(p.manifest, '$.reference_sequence') = e.sequence"
        )
    schema_editor.execute(
        f'''UPDATE {state} SET revoked = TRUE WHERE NOT revoked AND EXISTS (
      SELECT 1 FROM {binding} b JOIN {proof} p ON p.assignment_id = b.assignment_id_frozen
        AND p.manifest_hash = b.proof_hash
      WHERE b.assignment_id_frozen = {state}.assignment_id AND b.purpose = 'quality'
        AND (NOT EXISTS (SELECT 1 FROM {event} e WHERE e.kind = 'account'
                         AND e.subject_id = b.actor_id_frozen AND {account_match})
          OR NOT EXISTS (SELECT 1 FROM {event} e WHERE e.kind = 'reference'
                         AND e.subject_id = b.reference_id_frozen AND {reference_match})))'''
    )
    if vendor == 'postgresql':
        schema_editor.execute(
            f'''CREATE TRIGGER blind_read_identity_state_guard BEFORE INSERT OR UPDATE OR DELETE ON {state}
          FOR EACH ROW EXECUTE FUNCTION blind_read_identity_state_guard()'''
        )


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0060_blind_account_exposure_state')]
    operations = [migrations.RunPython(reconcile, migrations.RunPython.noop)]
