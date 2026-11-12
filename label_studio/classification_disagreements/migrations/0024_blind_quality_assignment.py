from django.db import migrations, models


def install(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor not in ('postgresql', 'sqlite'):
        return
    q = schema_editor.quote_name
    marker = q(apps.get_model('classification_disagreements', 'BlindQualityAssignment')._meta.db_table)
    proof = q(apps.get_model('classification_disagreements', 'BlindEligibilityProof')._meta.db_table)
    reference = q(apps.get_model('classification_disagreements', 'ClassificationGoldEvidence')._meta.db_table)
    assignment = q(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
    task = q(apps.get_model('tasks', 'Task')._meta.db_table)
    project = q(apps.get_model('projects', 'Project')._meta.db_table)
    annotation = q(apps.get_model('tasks', 'Annotation')._meta.db_table)
    draft = q(apps.get_model('tasks', 'AnnotationDraft')._meta.db_table)
    submission = q(apps.get_model('tasks', 'Submission')._meta.db_table)
    review = q(apps.get_model('tasks', 'ReviewDecision')._meta.db_table)
    history = q(apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table)
    eligible = f"""EXISTS (
        SELECT 1 FROM {assignment} a WHERE a.id = NEW.assignment_id
        AND a.task_id = NEW.task_id AND a.project_id = NEW.project_id AND a.assignee_id = NEW.assignee_id
        AND EXISTS (SELECT 1 FROM {history} h WHERE h.account_id = a.assignee_id AND h.elevated = FALSE)
        AND ((NEW.reference_id IS NULL AND NEW.reference_hash IS NULL) OR
             (NEW.reference_id IS NOT NULL AND LENGTH(NEW.reference_hash) = 64
              AND EXISTS (SELECT 1 FROM {reference} r WHERE r.id = NEW.reference_id
                          AND r.project_id = a.project_id AND r.kind = 'reference'
                          AND r.manifest_hash = NEW.reference_hash)))
        AND NOT EXISTS (SELECT 1 FROM {marker} prior_ref WHERE prior_ref.task_id = a.task_id
                        AND (COALESCE(prior_ref.reference_id, 0) <> COALESCE(NEW.reference_id, 0)
                             OR COALESCE(prior_ref.reference_hash, '') <> COALESCE(NEW.reference_hash, '')))
        AND a.status = 'assigned' AND a.annotation_id IS NULL
        AND EXISTS (SELECT 1 FROM {task} t WHERE t.id = a.task_id AND t.project_id = a.project_id)
        AND NOT EXISTS (SELECT 1 FROM {draft} d WHERE d.assignment_id = a.id)
        AND NOT EXISTS (SELECT 1 FROM {submission} s WHERE s.assignment_id = a.id)
        AND NOT EXISTS (SELECT 1 FROM {annotation} n
                        WHERE n.task_id = a.task_id AND n.completed_by_id = a.assignee_id)
        AND NOT EXISTS (SELECT 1 FROM {marker} seen
                        WHERE seen.task_id = a.task_id AND seen.assignee_id = a.assignee_id)
        AND (
          (NOT EXISTS (SELECT 1 FROM {marker} prior WHERE prior.task_id = a.task_id)
           AND NOT EXISTS (SELECT 1 FROM {assignment} other WHERE other.task_id = a.task_id AND other.id <> a.id)
           AND NOT EXISTS (SELECT 1 FROM {draft} d WHERE d.task_id = a.task_id)
           AND NOT EXISTS (SELECT 1 FROM {annotation} n WHERE n.task_id = a.task_id))
          OR
          (EXISTS (SELECT 1 FROM {marker} prior WHERE prior.task_id = a.task_id AND prior.project_id = a.project_id)
           AND NOT EXISTS (SELECT 1 FROM {assignment} other
                           WHERE other.task_id = a.task_id AND other.id <> a.id
                           AND (other.status <> 'cancelled' OR NOT EXISTS
                             (SELECT 1 FROM {marker} historical WHERE historical.assignment_id = other.id))))
        )
    )"""
    if vendor == 'postgresql':
        schema_editor.execute(f'''
          CREATE FUNCTION blind_quality_marker_insert() RETURNS trigger LANGUAGE plpgsql AS $$
          DECLARE locked_task bigint; locked_project bigint; locked_assignee bigint;
          BEGIN
            SELECT task_id, project_id INTO locked_task, locked_project
              FROM {assignment} WHERE id = NEW.assignment_id;
            PERFORM id FROM {project} WHERE id = locked_project FOR UPDATE;
            PERFORM id FROM {task} WHERE id = locked_task FOR UPDATE;
            PERFORM id FROM {assignment} WHERE id = NEW.assignment_id FOR UPDATE;
            SELECT assignee_id INTO locked_assignee FROM {assignment} WHERE id = NEW.assignment_id;
            PERFORM account_id FROM {history} WHERE account_id = locked_assignee FOR UPDATE;
            IF NOT ({eligible}) OR NOT EXISTS (SELECT 1 FROM {assignment} a
                WHERE a.id = NEW.assignment_id AND a.task_id = locked_task AND a.project_id = locked_project) THEN
              RAISE EXCEPTION 'quality purpose requires untouched assignment';
            END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_marker_insert BEFORE INSERT ON {marker}
          FOR EACH ROW EXECUTE FUNCTION blind_quality_marker_insert()''')
        schema_editor.execute('''CREATE FUNCTION blind_quality_marker_immutable()
          RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'quality purpose is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_marker_immutable BEFORE UPDATE OR DELETE ON {marker}
          FOR EACH ROW EXECUTE FUNCTION blind_quality_marker_immutable()''')
        schema_editor.execute(f'''
          CREATE FUNCTION blind_quality_review_guard() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF EXISTS (SELECT 1 FROM {submission} s JOIN {assignment} a ON a.id = s.assignment_id
                       JOIN {marker} m ON m.task_id = a.task_id
                       WHERE s.id = NEW.submission_id) THEN
              RAISE EXCEPTION 'quality submission cannot be reviewed';
            END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_review_guard BEFORE INSERT OR UPDATE ON {review}
          FOR EACH ROW EXECUTE FUNCTION blind_quality_review_guard()''')
        schema_editor.execute(f'''
          CREATE FUNCTION blind_quality_status_guard() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF NEW.status IN ('approved', 'rejected')
               AND EXISTS (SELECT 1 FROM {assignment} a JOIN {marker} m ON m.task_id = a.task_id
                           WHERE a.id = NEW.assignment_id) THEN
              RAISE EXCEPTION 'quality submission cannot be reviewed';
            END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_status_guard BEFORE INSERT OR UPDATE ON {submission}
          FOR EACH ROW EXECUTE FUNCTION blind_quality_status_guard()''')
        schema_editor.execute(f'''
          CREATE FUNCTION blind_quality_assignment_guard() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF TG_OP = 'UPDATE' THEN
              IF OLD.status = 'cancelled' AND NEW.status IN ('assigned', 'in_progress')
                 AND EXISTS (SELECT 1 FROM {marker} m WHERE m.assignment_id = OLD.id) THEN
                RAISE EXCEPTION 'cancelled quality assignment cannot be reactivated';
              END IF;
              IF (NEW.task_id, NEW.project_id, NEW.assignee_id) IS DISTINCT FROM
                 (OLD.task_id, OLD.project_id, OLD.assignee_id)
                 AND EXISTS (SELECT 1 FROM {marker} m WHERE m.task_id IN (OLD.task_id, NEW.task_id)) THEN
                RAISE EXCEPTION 'quality assignment scope is immutable';
              END IF;
              IF NEW.task_id IS NOT DISTINCT FROM OLD.task_id THEN RETURN NEW; END IF;
            END IF;
            PERFORM id FROM {task} WHERE id = NEW.task_id FOR UPDATE;
            IF EXISTS (SELECT 1 FROM {marker} m WHERE m.task_id = NEW.task_id)
               AND EXISTS (SELECT 1 FROM {assignment} a WHERE a.task_id = NEW.task_id
                           AND a.status IN ('assigned', 'in_progress')) THEN
              RAISE EXCEPTION 'quality task already has an active assignment';
            END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_assignment_guard BEFORE INSERT OR UPDATE ON {assignment}
          FOR EACH ROW EXECUTE FUNCTION blind_quality_assignment_guard()''')
        schema_editor.execute(f'''
          CREATE FUNCTION blind_quality_reassignment_marker() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF EXISTS (SELECT 1 FROM {marker} m WHERE m.task_id = NEW.task_id) THEN
              INSERT INTO {marker} (assignment_id, task_id, project_id, assignee_id,
                                    reference_id, reference_hash, created_at)
                SELECT NEW.id, NEW.task_id, NEW.project_id, NEW.assignee_id,
                       prior.reference_id, prior.reference_hash, CURRENT_TIMESTAMP
                FROM {marker} prior WHERE prior.task_id = NEW.task_id ORDER BY prior.id LIMIT 1;
            END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_reassignment_marker AFTER INSERT ON {assignment}
          FOR EACH ROW EXECUTE FUNCTION blind_quality_reassignment_marker()''')
        schema_editor.execute(f'''
          CREATE FUNCTION blind_assignment_history() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            UPDATE {history} SET had_assignment = TRUE,
              first_assignment_id = COALESCE(first_assignment_id, NEW.id) WHERE account_id = NEW.assignee_id;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_assignment_history AFTER INSERT OR UPDATE OF assignee_id ON {assignment}
          FOR EACH ROW EXECUTE FUNCTION blind_assignment_history()''')
        schema_editor.execute(f'''
          CREATE FUNCTION blind_active_privilege_guard() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF NEW.elevated AND NOT OLD.elevated AND EXISTS (
              SELECT 1 FROM {assignment} a JOIN {marker} m ON m.assignment_id = a.id
              WHERE a.assignee_id = OLD.account_id AND a.status IN ('assigned', 'in_progress')
            ) THEN RAISE EXCEPTION 'active quality assignment prevents privilege elevation'; END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_active_privilege_guard BEFORE UPDATE ON {history}
          FOR EACH ROW EXECUTE FUNCTION blind_active_privilege_guard()''')
        schema_editor.execute(f'''
          CREATE FUNCTION blind_quality_annotation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF TG_OP = 'UPDATE' THEN
              IF (NEW.task_id, NEW.project_id) IS DISTINCT FROM (OLD.task_id, OLD.project_id)
                 AND EXISTS (SELECT 1 FROM {marker} m WHERE m.task_id IN (OLD.task_id, NEW.task_id)) THEN
                RAISE EXCEPTION 'quality annotation scope is immutable';
              END IF;
            ELSIF EXISTS (SELECT 1 FROM {marker} m WHERE m.task_id = NEW.task_id)
                  AND NOT EXISTS (SELECT 1 FROM {marker} m JOIN {assignment} a ON a.id = m.assignment_id
                    WHERE m.task_id = NEW.task_id AND m.project_id = NEW.project_id
                      AND a.task_id = NEW.task_id AND a.project_id = NEW.project_id
                      AND a.assignee_id = NEW.completed_by_id AND m.assignee_id = NEW.completed_by_id
                      AND a.status IN ('assigned', 'in_progress')) THEN
              RAISE EXCEPTION 'quality annotation requires its active assignment';
            END IF;
            RETURN NEW;
          END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_annotation_guard BEFORE INSERT OR UPDATE ON {annotation}
          FOR EACH ROW EXECUTE FUNCTION blind_quality_annotation_guard()''')
        schema_editor.execute('''CREATE FUNCTION blind_eligibility_proof_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'quality eligibility proof is immutable'; END $$''')
        schema_editor.execute(f'''CREATE TRIGGER blind_eligibility_proof_immutable BEFORE UPDATE OR DELETE ON {proof}
          FOR EACH ROW EXECUTE FUNCTION blind_eligibility_proof_immutable()''')
    else:
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_marker_insert BEFORE INSERT ON {marker}
          WHEN NOT ({eligible}) BEGIN SELECT RAISE(ABORT, 'quality purpose requires untouched assignment'); END''')
        for operation in ('UPDATE', 'DELETE'):
            schema_editor.execute(f'''CREATE TRIGGER blind_quality_marker_{operation.lower()}
              BEFORE {operation} ON {marker}
              BEGIN SELECT RAISE(ABORT, 'quality purpose is immutable'); END''')
        for operation in ('INSERT', 'UPDATE'):
            schema_editor.execute(f'''CREATE TRIGGER blind_quality_review_{operation.lower()}
              BEFORE {operation} ON {review}
              WHEN EXISTS (SELECT 1 FROM {submission} s JOIN {assignment} a ON a.id = s.assignment_id
                           JOIN {marker} m ON m.task_id = a.task_id
                           WHERE s.id = NEW.submission_id)
              BEGIN SELECT RAISE(ABORT, 'quality submission cannot be reviewed'); END''')
            schema_editor.execute(f'''CREATE TRIGGER blind_quality_status_{operation.lower()}
              BEFORE {operation} ON {submission}
              WHEN NEW.status IN ('approved', 'rejected')
                   AND EXISTS (SELECT 1 FROM {assignment} a JOIN {marker} m ON m.task_id = a.task_id
                               WHERE a.id = NEW.assignment_id)
              BEGIN SELECT RAISE(ABORT, 'quality submission cannot be reviewed'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_assignment_insert BEFORE INSERT ON {assignment}
          WHEN EXISTS (SELECT 1 FROM {marker} m WHERE m.task_id = NEW.task_id)
               AND EXISTS (SELECT 1 FROM {assignment} a WHERE a.task_id = NEW.task_id
                           AND a.status IN ('assigned', 'in_progress'))
          BEGIN SELECT RAISE(ABORT, 'quality task already has an active assignment'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_reassignment_marker AFTER INSERT ON {assignment}
          WHEN EXISTS (SELECT 1 FROM {marker} m WHERE m.task_id = NEW.task_id)
          BEGIN INSERT INTO {marker} (assignment_id, task_id, project_id, assignee_id,
                                      reference_id, reference_hash, created_at)
            SELECT NEW.id, NEW.task_id, NEW.project_id, NEW.assignee_id,
                   prior.reference_id, prior.reference_hash, CURRENT_TIMESTAMP
            FROM {marker} prior WHERE prior.task_id = NEW.task_id ORDER BY prior.id LIMIT 1; END''')
        for suffix, event in (('insert', 'INSERT'), ('update', 'UPDATE OF assignee_id')):
            schema_editor.execute(f'''CREATE TRIGGER blind_assignment_history_{suffix} AFTER {event} ON {assignment}
              BEGIN UPDATE {history} SET had_assignment = TRUE,
                first_assignment_id = COALESCE(first_assignment_id, NEW.id) WHERE account_id = NEW.assignee_id; END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_active_privilege_guard BEFORE UPDATE ON {history}
          WHEN NEW.elevated AND NOT OLD.elevated AND EXISTS (
            SELECT 1 FROM {assignment} a JOIN {marker} m ON m.assignment_id = a.id
            WHERE a.assignee_id = OLD.account_id AND a.status IN ('assigned', 'in_progress'))
          BEGIN SELECT RAISE(ABORT, 'active quality assignment prevents privilege elevation'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_assignment_update BEFORE UPDATE ON {assignment}
          WHEN (OLD.status = 'cancelled' AND NEW.status IN ('assigned', 'in_progress')
                AND EXISTS (SELECT 1 FROM {marker} m WHERE m.assignment_id = OLD.id))
               OR ((NEW.task_id IS NOT OLD.task_id OR NEW.project_id IS NOT OLD.project_id
                 OR NEW.assignee_id IS NOT OLD.assignee_id)
                AND EXISTS (SELECT 1 FROM {marker} m WHERE m.task_id = OLD.task_id))
               OR (NEW.task_id IS NOT OLD.task_id
                   AND EXISTS (SELECT 1 FROM {marker} m WHERE m.task_id = NEW.task_id))
          BEGIN SELECT RAISE(ABORT, 'quality assignment scope is immutable'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_annotation_insert BEFORE INSERT ON {annotation}
          WHEN EXISTS (SELECT 1 FROM {marker} m WHERE m.task_id = NEW.task_id)
               AND NOT EXISTS (SELECT 1 FROM {marker} m JOIN {assignment} a ON a.id = m.assignment_id
                 WHERE m.task_id = NEW.task_id AND m.project_id = NEW.project_id
                   AND a.task_id = NEW.task_id AND a.project_id = NEW.project_id
                   AND a.assignee_id = NEW.completed_by_id AND m.assignee_id = NEW.completed_by_id
                   AND a.status IN ('assigned', 'in_progress'))
          BEGIN SELECT RAISE(ABORT, 'quality annotation requires its active assignment'); END''')
        schema_editor.execute(f'''CREATE TRIGGER blind_quality_annotation_update BEFORE UPDATE ON {annotation}
          WHEN (NEW.task_id IS NOT OLD.task_id OR NEW.project_id IS NOT OLD.project_id)
               AND EXISTS (SELECT 1 FROM {marker} m WHERE m.task_id IN (OLD.task_id, NEW.task_id))
          BEGIN SELECT RAISE(ABORT, 'quality annotation scope is immutable'); END''')
        for operation in ('UPDATE', 'DELETE'):
            schema_editor.execute(f'''CREATE TRIGGER blind_eligibility_proof_{operation.lower()} BEFORE {operation} ON {proof}
              BEGIN SELECT RAISE(ABORT, 'quality eligibility proof is immutable'); END''')


def uninstall(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'postgresql':
        marker = schema_editor.quote_name(
            apps.get_model('classification_disagreements', 'BlindQualityAssignment')._meta.db_table)
        review = schema_editor.quote_name(apps.get_model('tasks', 'ReviewDecision')._meta.db_table)
        submission = schema_editor.quote_name(apps.get_model('tasks', 'Submission')._meta.db_table)
        assignment = schema_editor.quote_name(apps.get_model('tasks', 'TaskAssignment')._meta.db_table)
        annotation = schema_editor.quote_name(apps.get_model('tasks', 'Annotation')._meta.db_table)
        history = schema_editor.quote_name(
            apps.get_model('classification_disagreements', 'BlindPrivilegeHistory')._meta.db_table)
        proof = schema_editor.quote_name(
            apps.get_model('classification_disagreements', 'BlindEligibilityProof')._meta.db_table)
        for name, table in (('blind_quality_marker_insert', marker), ('blind_quality_marker_immutable', marker),
                            ('blind_quality_review_guard', review), ('blind_quality_status_guard', submission),
                            ('blind_quality_assignment_guard', assignment),
                            ('blind_quality_reassignment_marker', assignment),
                            ('blind_assignment_history', assignment),
                            ('blind_active_privilege_guard', history),
                            ('blind_quality_annotation_guard', annotation),
                            ('blind_eligibility_proof_immutable', proof)):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}')
            schema_editor.execute(f'DROP FUNCTION IF EXISTS {name}()')
    elif vendor == 'sqlite':
        for name in ('blind_quality_marker_insert', 'blind_quality_marker_update', 'blind_quality_marker_delete',
                     'blind_quality_review_insert', 'blind_quality_review_update',
                     'blind_quality_status_insert', 'blind_quality_status_update',
                     'blind_quality_assignment_insert', 'blind_quality_assignment_update',
                     'blind_quality_reassignment_marker', 'blind_assignment_history_insert',
                     'blind_assignment_history_update', 'blind_active_privilege_guard',
                     'blind_quality_annotation_insert', 'blind_eligibility_proof_update', 'blind_eligibility_proof_delete',
                     'blind_quality_annotation_update'):
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}')


class Migration(migrations.Migration):
    dependencies = [('classification_disagreements', '0023_blind_capture_epoch'),
                    ('tasks', '0063_annotationdraft_revision')]
    operations = [
        migrations.CreateModel(name='BlindEligibilityProof', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('assignment_id', models.PositiveBigIntegerField(unique=True)),
            ('manifest', models.JSONField()),
            ('manifest_hash', models.CharField(max_length=64)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
        ]),
        migrations.CreateModel(name='BlindQualityAssignment', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('assignment_id', models.PositiveBigIntegerField(unique=True)),
            ('task_id', models.PositiveBigIntegerField(db_index=True)),
            ('project_id', models.PositiveBigIntegerField(db_index=True)),
            ('assignee_id', models.PositiveBigIntegerField()),
            ('reference_id', models.PositiveBigIntegerField(null=True)),
            ('reference_hash', models.CharField(max_length=64, null=True)),
        ], options={'constraints': [
            models.CheckConstraint(condition=(models.Q(reference_id__isnull=True, reference_hash__isnull=True)
                | models.Q(reference_id__isnull=False, reference_hash__isnull=False)),
                name='blind_quality_reference_pair'),
        ]}),
        migrations.RunPython(install, uninstall),
    ]
