"""Bounded single-batch graph. No current policy refresh, scoring or answer export."""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

from classification_disagreements import arbitration
from classification_disagreements import service as classification
from classification_disagreements.models import ArbitrationDecision, ArbitrationTask
from django.db import connection, models
from django.db.models.expressions import RawSQL
from rest_framework.exceptions import APIException
from review_batches import confidence_contract as types
from review_batches import confidence_priority, followup_records, handoff, priority, second_review_service, service
from review_batches.models import (
    ReviewArbitrationHandoff,
    ReviewBatch,
    SecondReviewConcernFollowup,
    SecondReviewDecision,
    SecondReviewTask,
)
from tasks.models import ReviewDecision, Submission, Task, TaskAssignment

from . import audit_contract as c
from .approved import stamp


@contextmanager
def query_budget():
    used = 0

    def execute(execute, sql, params, many, context):
        nonlocal used
        if sql.lstrip().upper().startswith(('SELECT', 'WITH')):
            used += 1
            if used > c.limits()['sql_per_pass']:
                c.fail('SQL_BUDGET', 422)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(execute):
        yield


class Graph:
    def __init__(self, budget):
        self.budget, self.facts, self.edges = budget, {}, set()
        self.gaps = Counter()
        self.source_ids = {}
        self.minimum_entities = 0
        self.snapshot_canonical = 0

    def reserve_entities(self, count):
        self.minimum_entities += count
        if self.minimum_entities > c.limits()['entities']:
            c.fail('TOO_LARGE', 422)

    def scalars(self, values):
        serialized = [str(v) if not isinstance(v, (int, str, type(None))) else v for v in values]
        self.budget.canonical += len(c.exact(serialized))
        self.budget.stored += sum(len(str(v).encode()) for v in serialized if v is not None)
        if self.budget.canonical > self.budget.canonical_limit or self.budget.stored > self.budget.stored_limit:
            c.fail('TOO_LARGE', 422)

    def hints(self, query, field, *, uuid=False):
        count = query.count()
        self.scalars([count])
        if count > c.limits()['entities']:
            c.fail('TOO_LARGE', 422)
        if count == 0:
            return set()
        measured = models.functions.Cast(
            models.F(field), models.TextField() if connection.vendor == 'postgresql' else models.BinaryField()
        )
        measured = models.Func(
            measured,
            function='OCTET_LENGTH' if connection.vendor == 'postgresql' else 'LENGTH',
            output_field=models.BigIntegerField(),
        )
        bounded = query.order_by('pk').annotate(_hint_size=measured)[: c.limits()['entities'] + 1]
        sizes = list(bounded.values_list('_hint_size', flat=True))
        self.scalars(sizes)
        if any(type(size) is not int or not 1 <= size <= 128 for size in sizes):
            c.fail()
        bounded = bounded.annotate(
            _hint_value=models.Case(
                models.When(_hint_size__lte=128, then=models.functions.Cast(models.F(field), models.TextField())),
                default=models.Value(None),
                output_field=models.TextField(),
            )
        )
        values = list(bounded.values_list('_hint_value', flat=True))
        self.scalars(values)
        try:
            parsed = [UUID(value) if uuid else int(value) for value in values]
        except (TypeError, ValueError, AttributeError):
            c.fail()
        invalid = (
            any(value not in (str(v), v.hex) for value, v in zip(values, parsed))
            if uuid
            else any(not c.positive(v) or str(v) != value for value, v in zip(values, parsed))
        )
        if len(values) > c.limits()['entities'] or invalid:
            c.fail()
        return set(parsed)

    def rows(self, query, payloads=None, *, cap=None, lock=False):
        cap = min(cap or c.limits()['entities'], c.limits()['entities'])
        count = query.count()
        self.scalars([count])
        if count > cap:
            c.fail('TOO_LARGE', 422)
        if query.model is Submission:
            table = connection.ops.quote_name(query.model._meta.db_table)
            col = table + '.' + connection.ops.quote_name(query.model._meta.get_field('result_snapshot').column)
            measure = (
                f'OCTET_LENGTH(CAST({col} AS TEXT))'
                if connection.vendor == 'postgresql'
                else f'LENGTH(CAST({col} AS BLOB))'
            )
            stored = (
                query.aggregate(total=models.Sum(RawSQL(measure, [], output_field=models.BigIntegerField())))['total']
                or 0
            )
            self.scalars([stored])
            if stored > 2 * c.MIB:
                c.fail('TOO_LARGE', 422)
        if query.model in (
            Submission,
            ReviewDecision,
            SecondReviewTask,
            SecondReviewDecision,
            SecondReviewConcernFollowup,
            ReviewArbitrationHandoff,
            ArbitrationDecision,
        ):
            self.reserve_entities(count)
        ids = list(query.order_by('pk').values_list('pk', flat=True)[: cap + 1])
        self.scalars(ids)
        if len(ids) > cap:
            c.fail('TOO_LARGE', 422)
        result = []
        for offset in range(0, len(ids), c.limits()['chunk']):
            part = query.filter(pk__in=ids[offset : offset + c.limits()['chunk']]).order_by('pk')
            if lock:
                # PostgreSQL forbids FOR UPDATE on a windowed SELECT. Lock only
                # scalar IDs, then hydrate through the existing SQL CASE gates.
                self.scalars(list(part.select_for_update().values_list('pk', flat=True)))
            if connection.vendor == 'sqlite':
                table = connection.ops.quote_name(query.model._meta.db_table)
                for field in query.model._meta.fields:
                    if isinstance(field, models.JSONField) and field.name in (payloads or {}):
                        col = table + '.' + connection.ops.quote_name(field.column)
                        flags = list(
                            part.annotate(
                                _audit_unique=RawSQL(
                                    f"NOT EXISTS(SELECT 1 FROM json_tree({col}) j WHERE typeof(j.key)='text' GROUP BY j.parent,j.key HAVING count(*)>1)",
                                    [],
                                )
                            ).values_list('_audit_unique', flat=True)
                        )
                        self.scalars(flags)
                        if any(flag != 1 for flag in flags):
                            c.fail()
            try:
                rows = self.budget.rows(
                    part,
                    payloads or {},
                    union=(512 * 1024, 2 * c.MIB) if query.model is Submission else None,
                    observe=self.scalars,
                )
            except RecursionError:
                c.fail('TOO_LARGE', 422)
            for row in rows:
                if query.model is Submission:
                    self.snapshot_canonical += len(c.exact(row.result_snapshot))
                    if self.snapshot_canonical > 512 * 1024:
                        c.fail('TOO_LARGE', 422)
                for field in row._meta.fields:
                    if isinstance(field, models.DateTimeField) and field.attname in row.__dict__:
                        value = getattr(row, field.attname)
                        if value is None and field.null:
                            continue
                        if not isinstance(value, datetime) or value.tzinfo is None:
                            c.fail()
            result.extend(rows)
        if len(result) != len(ids):
            c.fail('SOURCE_CHANGED')
        return result

    def add(self, kind, key, public, bindings=None, *, gap=None):
        identifier = (kind, str(key))
        if identifier in self.facts:
            c.fail()
        if len(self.facts) >= c.limits()['entities']:
            c.fail('TOO_LARGE', 422)
        if gap:
            self.gaps[gap] += 1
            public = {**public, 'gap': gap}
        self.facts[identifier] = dict(type=kind, key=str(key), public=public, bindings=bindings or {})
        return identifier

    def link(self, left, right, relation):
        if left not in self.facts or right not in self.facts:
            c.fail()
        self.edges.add((left, right, relation))
        if len(self.edges) > c.limits()['edges']:
            c.fail('TOO_LARGE', 422)

    def private(self):
        return dict(
            facts=[self.facts[k] for k in sorted(self.facts)],
            edges=[dict(source=list(a), target=list(b), relation=r) for a, b, r in sorted(self.edges)],
            gaps=dict(sorted(self.gaps.items())),
        )

    def project(self, ref):
        records = []
        for (kind, key), fact in sorted(self.facts.items()):
            record = dict(type=kind, id=ref(kind, key), **fact['public'])
            if fact['bindings']:
                record['source_commitment'] = ref('commitment', fact['bindings'])
            if fact['bindings'].get('actor_occurrence') is not None:
                record['actor_ref'] = ref('actor_occurrence', fact['bindings']['actor_occurrence'])
            records.append(record)
        for a, b, relation in sorted(self.edges):
            records.append(dict(type='edge', source=ref(*a), target=ref(*b), relation=relation))
        return sorted(
            records,
            key=lambda r: (r['type'], r.get('id', r.get('source', '')), r.get('target', ''), r.get('relation', '')),
        )


def plan(graph, project, batch_id):
    rows = graph.rows(
        ReviewBatch.objects.filter(project=project, pk=batch_id).only(
            'id',
            'project_id',
            'actor_id_frozen',
            'creator_principal',
            'created_at',
            'request_hash',
            'plan',
            'plan_hash',
        ),
        {'plan': (4 * c.MIB, 16 * c.MIB)},
        cap=1,
    )
    if not rows:
        c.fail('ROOT_UNAVAILABLE', 404)
    batch = rows[0]
    p = batch.plan
    try:
        if (
            not isinstance(p, dict)
            or set(p)
            not in (
                {'algorithm', 'project_id', 'rule', 'candidates'},
                {'algorithm', 'project_id', 'rule', 'candidates', 'priority'},
            )
            or p['project_id'] != project.pk
            or type(p['project_id']) is not int
            or service.digest(p) != batch.plan_hash
        ):
            c.fail()
        candidates = p['candidates']
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= c.limits()['roots']:
            c.fail('TOO_LARGE', 422)
        fields = {'submission_id', 'assignment_id', 'task_id', 'revision', 'result_hash', 'submitter_id', 'selected'}
        if any(
            not isinstance(v, dict)
            or set(v) != fields
            or type(v['selected']) is not bool
            or not c.hexadecimal(v['result_hash'])
            or any(not c.positive(v[k]) for k in fields - {'result_hash', 'submitter_id', 'selected'})
            or v['submitter_id'] is not None
            and not c.positive(v['submitter_id'])
            for v in candidates
        ):
            c.fail()
        if len({v['submission_id'] for v in candidates}) != len(candidates):
            c.fail()
        if p['algorithm'] not in (service.ALGORITHM, priority.ALGORITHM, confidence_priority.ALGORITHM):
            c.fail('UNSUPPORTED_INPUT', 400)
        if 'priority' in p:
            priority.validate(p, batch)
        else:
            rule = p['rule']
            if (
                set(rule) != {'rate', 'per_submitter', 'seed', 'package_ids', 'submitter_ids'}
                or not types.integer(rule['rate'], 1, 100)
                or type(rule['per_submitter']) is not bool
                or not types.text(rule['seed'], 128)
                or any(
                    not isinstance(rule[k], list) or len(rule[k]) > 100 or any(not c.positive(v) for v in rule[k])
                    for k in ('package_ids', 'submitter_ids')
                )
            ):
                c.fail()
            rebuilt = service.assemble_plan(
                project.pk, rule, [{k: v for k, v in item.items() if k != 'selected'} for item in candidates]
            )
            if service.digest(rebuilt) != batch.plan_hash:
                c.fail()
    except (KeyError, TypeError, ValueError, AttributeError):
        c.fail()
    return batch


def review_proof(review):
    return dict(
        schema='first-review-proof/v1',
        id=review.pk,
        submission_id=review.submission_id,
        reviewer_id=review.reviewer_id,
        decision=review.decision,
        reason=review.reason,
        created_at=stamp(review.created_at),
    )


def source_metadata(proof):
    """Retain typed provenance, never the frozen free initial-review reason."""
    return {k: v for k, v in proof.items() if k != 'first_review'}


def checked_second(row, project, batch):
    p = row.proof
    try:
        if (
            row.project_id != project.pk
            or row.organization_id_frozen != project.organization_id
            or row.review_batch_id != batch.pk
            or row.review_batch_id_frozen != batch.pk
            or row.submission_id not in (None, row.submission_id_frozen)
            or service.digest(p) != row.source_hash
            or service.digest(row.receipt) != row.receipt_hash
            or p['schema'] != 'second-review-source/v1'
            or p['rule'] != 'independent-supervision/v1'
            or p['input_contract'] != 'static-text-choices/v1'
            or p['effect'] != c.EFFECT
            or p['project_id'] != project.pk
            or p['organization_id'] != project.organization_id
            or p['batch_id'] != batch.pk
            or p['plan_hash'] != batch.plan_hash
            or p['subject']['submission_id'] != row.submission_id_frozen
            or p['subject']['revision'] != row.revision
            or p['instance_id'] != str(row.instance_id)
            or p['first_review_hash'] != row.first_review_hash
            or service.digest(p['first_review']) != row.first_review_hash
            or p['candidate']
            != next((v for v in batch.plan['candidates'] if v['submission_id'] == row.submission_id_frozen), None)
            or not followup_records.pins(p['subject'])
            or not followup_records.first(p['first_review'], p['subject'])
        ):
            c.fail()
        second_review_service.checked_receipt(row.receipt, 'dispatch', row, row.idempotency_key, row.request_hash)
        if row.status == 'cancelled':
            cancellation = row.cancellation
            if not isinstance(cancellation, dict) or service.digest(cancellation) != row.cancellation_hash:
                c.fail()
            ack = cancellation['receipt']
            second_review_service.checked_receipt(
                ack, 'cancel', row, row.cancel_key, service.digest(cancellation['body'])
            )
        elif row.cancellation is not None or row.cancel_key is not None or row.cancellation_hash:
            c.fail()
        if (row.status, row.version) not in (('pending', 1), ('completed', 2), ('cancelled', 2)):
            c.fail()
    except (KeyError, TypeError, AttributeError, ValueError):
        c.fail()
    return p


def retained_initial(g, subject, proofs):
    first = proofs[0]['first_review']
    if any(service.digest(p['first_review']) != service.digest(first) for p in proofs):
        c.fail()
    key = g.add(
        'initial_review',
        first['id'],
        dict(
            decision=first['decision'],
            created_at=first['created_at'],
            reason_state='present_redacted' if first['reason'] else 'absent',
            association='subject_observed',
            batch_execution_provenance='not_recorded',
            actor_incarnation='not_recorded',
            evidence='frozen_receipt_verified',
        ),
        dict(proof_hash=service.digest(first), actor_occurrence=[first['id'], 'reviewer', first['reviewer_id']]),
        gap='live_origin_unavailable',
    )
    g.link(subject, key, 'initial_review')


def load(project, batch_id, budget, *, lock=False):
    with query_budget():
        try:
            return _load(project, batch_id, budget, lock=lock)
        except APIException as exc:
            if str(exc.detail).startswith('AUDIT_'):
                raise
            reason = (
                'AUDIT_TOO_LARGE'
                if str(exc.detail)
                in ('SECOND_REVIEW_TOO_LARGE', 'REVIEW_PRIORITY_TOO_LARGE', 'REVIEW_CONFIDENCE_TOO_LARGE')
                else 'AUDIT_SOURCE_INTEGRITY_INVALID'
            )
            mapped = APIException(reason)
            mapped.status_code = 422 if reason == 'AUDIT_TOO_LARGE' else 409
            mapped.display_context = {'reason': reason}
            raise mapped from None


def _load(project, batch_id, budget, *, lock):
    g = Graph(budget)
    batch = plan(g, project, batch_id)
    candidates = batch.plan['candidates']
    g.reserve_entities(1 + len(candidates))
    assignments = {v['assignment_id'] for v in candidates}
    if len(assignments) > c.limits()['assignments']:
        c.fail('TOO_LARGE', 422)
    # No User/profile hydration. Every JSON/Text/scalar is gated before decoding.
    assignment_rows = g.rows(
        TaskAssignment.objects.filter(pk__in=assignments, project=project).only(
            'id', 'project_id', 'task_id', 'assignee_id'
        ),
        cap=c.limits()['assignments'],
    )
    by_assignment = {r.pk: r for r in assignment_rows}
    tasks = g.rows(
        Task.objects.filter(pk__in={v['task_id'] for v in candidates}, project=project).only('id', 'project_id'),
        cap=c.limits()['roots'],
    )
    task_ids = {v.pk for v in tasks}
    task_by_id = {v.pk: v for v in tasks}
    second_scope = SecondReviewTask.objects.filter(project=project, review_batch_id_frozen=batch.pk)
    followup_scope = SecondReviewConcernFollowup.objects.filter(project=project, origin_ref__source__batch_id=batch.pk)
    handoff_scope = ReviewArbitrationHandoff.objects.filter(project=project, review_batch_id_frozen=batch.pk)
    detached_tasks = g.hints(followup_scope.filter(second_review_task__isnull=True), 'task_id_frozen', uuid=True)
    second_scope = second_scope.exclude(pk__in=detached_tasks)
    detached_opinions = g.hints(followup_scope.filter(opinion__isnull=True), 'opinion_id_frozen', uuid=True)
    detached_sources = g.hints(second_scope.filter(submission__isnull=True), 'submission_id_frozen')
    detached_sources |= g.hints(
        followup_scope.filter(related_submission__isnull=True, related_submission_id_frozen__isnull=False),
        'related_submission_id_frozen',
    )
    detached_reviews = g.hints(second_scope.filter(first_review__isnull=True), 'proof__first_review__id')
    detached_arbitrations = g.hints(handoff_scope.filter(arbitration__isnull=True), 'arbitration_id_frozen')
    sources = g.rows(
        Submission.objects.filter(assignment_id__in=by_assignment)
        .exclude(pk__in=detached_sources)
        .only(
            'id',
            'assignment_id',
            'revision',
            'result_hash',
            'submitted_by_id',
            'submitted_at',
            'status',
            'result_snapshot',
        ),
        {'result_snapshot': (256 * 1024, 2 * c.MIB)},
        cap=c.limits()['revisions'],
    )
    if any(n > c.limits()['per_assignment'] for n in Counter(v.assignment_id for v in sources).values()):
        c.fail('TOO_LARGE', 422)
    g.reserve_entities(len(sources) - len({v.assignment_id for v in sources}))
    if lock:
        # Project/authority/human fences are already held by the command.
        for model, ids in ((Task, task_ids), (TaskAssignment, by_assignment), (Submission, [s.pk for s in sources])):
            for offset in range(0, len(ids), c.limits()['chunk']):
                ordered = sorted(ids)
                g.scalars(
                    list(
                        model.objects.filter(pk__in=ordered[offset : offset + c.limits()['chunk']])
                        .order_by('pk')
                        .select_for_update()
                        .values_list('pk', flat=True)
                    )
                )
    reviews = g.rows(
        ReviewDecision.objects.filter(submission_id__in=[s.pk for s in sources])
        .exclude(pk__in=detached_reviews)
        .only('id', 'submission_id', 'reviewer_id', 'decision', 'reason', 'created_at'),
        {'reason': (8192, 32768)},
        cap=c.limits()['revisions'],
        lock=lock,
    )
    by_review = {r.submission_id: r for r in reviews}
    if lock:
        g.scalars(list(ReviewBatch.objects.filter(pk=batch.pk).select_for_update().values_list('pk', flat=True)))
    links = g.rows(
        handoff_scope,
        {'origin': (512 * 1024, 2 * c.MIB)},
        lock=lock,
    )
    g.reserve_entities(len({r.arbitration_id_frozen for r in links}))
    targets = g.rows(
        ArbitrationTask.objects.filter(
            project=project, pk__in={r.arbitration_id for r in links if r.arbitration_id is not None}
        ).exclude(pk__in=detached_arbitrations),
        {
            'origin': (512 * 1024, 2 * c.MIB),
            'cancellation': (32 * 1024, 128 * 1024),
            'participant_ids': (16 * 1024, 64 * 1024),
        },
        lock=lock,
    )
    by_target = {r.pk: r for r in targets}
    decisions = g.rows(
        ArbitrationDecision.objects.filter(project=project, arbitration_id__in=by_target),
        {'winner': (16 * 1024, 64 * 1024), 'reason': (8192, 32768)},
        lock=lock,
    )
    by_decision = {r.arbitration_id: r for r in decisions}
    second = g.rows(
        second_scope.exclude(pk__in=detached_tasks),
        {'proof': (16 * 1024, 64 * 1024), 'receipt': (16 * 1024, 64 * 1024), 'cancellation': (32 * 1024, 128 * 1024)},
        lock=lock,
    )
    followups = g.rows(
        followup_scope,
        followup_records.PAYLOADS,
        lock=lock,
    )
    retained = {}
    second_by_id = {r.pk: r for r in second}
    for row in second:
        proof = checked_second(row, project, batch)
        retained.setdefault(row.submission_id_frozen, []).append(proof)
    for row in followups:
        followup_records.checked(row)
        proof = row.origin_ref['source']
        if (
            batch.created_at > row.opinion_created_at
            or proof['plan_hash'] != batch.plan_hash
            or proof['batch_id'] != batch.pk
            or proof['candidate']
            != next((v for v in candidates if v['submission_id'] == proof['subject']['submission_id']), None)
        ):
            c.fail()
        retained.setdefault(proof['subject']['submission_id'], []).append(proof)
    root = g.add(
        'batch',
        batch.pk,
        dict(
            algorithm=batch.plan['algorithm'],
            candidate_count=len(candidates),
            selection_rule='frozen-batch/v1',
            history_completeness='not_provable',
        ),
        dict(
            id=batch.pk,
            plan=batch.plan,
            request_hash=batch.request_hash,
            creator=batch.creator_principal,
            created_at=stamp(batch.created_at),
        ),
    )
    source_by_id = {r.pk: r for r in sources}
    public_sources = {}
    config_proofs = {}
    for source in sorted(sources, key=lambda r: (r.assignment_id, r.revision)):
        assignment = by_assignment[source.assignment_id]
        if assignment.task_id not in task_ids:
            c.fail('SOURCE_INTEGRITY_INVALID')
        assignment.task = task_by_id[assignment.task_id]
        source.assignment = assignment
        snapshot = source.result_snapshot
        try:
            if not service.valid_snapshot_scope(source, project.pk):
                c.fail()
            if (
                not types.supported(source)
                or sum(
                    node.tag == 'Text'
                    for node in classification.etree.fromstring(snapshot['project']['label_config']).iter()
                )
                != 1
            ):
                c.fail('UNSUPPORTED_INPUT', 400)
            pins = {
                **service.identity(source),
                'assignee_id': assignment.assignee_id,
                'task_data_hash': service.digest(snapshot['task']['data']),
                'label_config_sha256': c.digest(snapshot['project']['label_config'].encode()),
            }
            config_proofs.setdefault(
                (source.assignment_id, pins['label_config_sha256']),
                dict(
                    contract='single-static-text-choices/v1',
                    pins=pins,
                    canonicalization='python-json-sort-keys-compact-utf8/v1',
                ),
            )
            review = by_review.get(source.pk)
            if source.status not in ('pending', 'superseded', 'approved', 'rejected'):
                c.fail()
            if review and (review.decision != source.status or source.status not in ('approved', 'rejected')):
                c.fail()
            for proof in retained.get(source.pk, []):
                if source.status != proof['first_review']['decision']:
                    c.fail()
                if any(
                    proof['subject'][k] != v for k, v in pins.items() if k not in ('submitter_id', 'assignee_id')
                ) or any(
                    v is not None and proof['subject'][k] != v
                    for k, v in pins.items()
                    if k in ('submitter_id', 'assignee_id')
                ):
                    c.fail()
                if review:
                    current = review_proof(review)
                    if any(current[k] != v for k, v in proof['first_review'].items() if k != 'reviewer_id') or (
                        current['reviewer_id'] is not None
                        and current['reviewer_id'] != proof['first_review']['reviewer_id']
                    ):
                        c.fail()
        except (KeyError, TypeError, AttributeError, ValueError):
            c.fail()
        key = g.add(
            'revision',
            source.pk,
            dict(
                revision=source.revision,
                state=source.status,
                submitted_at=stamp(source.submitted_at),
                evidence='observed_at_capture',
            ),
            dict(
                pins=pins,
                input_contract='single-static-text-choices/v1',
                canonicalization='python-json-sort-keys-compact-utf8/v1',
            ),
        )
        public_sources[source.pk] = key
        g.link(root, key, 'root_assignment_revision')
        if review:
            observed = g.add(
                'initial_review',
                review.pk,
                dict(
                    decision=review.decision,
                    created_at=stamp(review.created_at),
                    reason_state='present_redacted' if review.reason else 'absent',
                    association='subject_observed',
                    batch_execution_provenance='not_recorded',
                    actor_incarnation='not_recorded',
                ),
                dict(
                    proof_hash=service.digest(review_proof(review)),
                    actor_occurrence=[review.pk, 'reviewer', review.reviewer_id],
                ),
            )
            g.link(key, observed, 'initial_review')
        elif retained.get(source.pk):
            retained_initial(g, key, retained[source.pk])
        elif source.status in ('approved', 'rejected'):
            missing = g.add(
                'gap',
                'initial:' + str(source.pk),
                dict(reason='initial_review_unavailable'),
                gap='initial_review_unavailable',
            )
            g.link(key, missing, 'initial_review_gap')
    confidence_evidence = {v['submission_id']: v for v in batch.plan.get('priority', {}).get('evidence', [])}
    for candidate in candidates:
        source = source_by_id.get(candidate['submission_id'])
        if source and (
            any(
                service.identity(source)[k] != v for k, v in candidate.items() if k not in ('selected', 'submitter_id')
            )
            or source.submitted_by_id is not None
            and source.submitted_by_id != candidate['submitter_id']
        ):
            c.fail()
        proofs = retained.get(candidate['submission_id'], [])
        if not source:
            if not proofs or any(any(p['candidate'].get(k) != v for k, v in candidate.items()) for p in proofs):
                c.fail('INPUT_SCOPE_UNPROVABLE', 422)
            for proof in proofs:
                if proof['input_contract'] != 'static-text-choices/v1':
                    c.fail('INPUT_SCOPE_UNPROVABLE', 422)
            # Old supervision receipts establish static Choices, but do not
            # establish Text arity. Content-addressed, scoped frozen XML may
            # prove only this type boundary; never borrow its answers/history.
            if any(p['subject'] != proofs[0]['subject'] for p in proofs):
                c.fail()
            type_proof = config_proofs.get((candidate['assignment_id'], proofs[0]['subject']['label_config_sha256']))
            if type_proof is None:
                c.fail('INPUT_SCOPE_UNPROVABLE', 422)
            missing = g.add(
                'revision',
                candidate['submission_id'],
                dict(revision=candidate['revision'], state='unavailable', evidence='frozen_receipt_verified'),
                dict(proofs=[source_metadata(p) for p in proofs], type_proof=type_proof),
                gap='live_origin_unavailable',
            )
            public_sources[candidate['submission_id']] = missing
            retained_initial(g, missing, proofs)
        e = confidence_evidence.get(candidate['submission_id'])
        reason = e.get('reason') if e else None
        # Only fixed source enums, never free producer/profile labels.
        allowed_reasons = {
            'frozen_conflict',
            'frozen_agreement',
            'not_covered',
            'revision_mismatch',
            'metric_unavailable',
            'frozen_low_confidence',
            'frozen_not_low_confidence',
            'unsupported_input',
            'revision_binding_changed',
            'source_unavailable',
        }
        if reason is not None and reason not in allowed_reasons:
            c.fail()
        node = g.add(
            'candidate',
            candidate['submission_id'],
            dict(
                selected=candidate['selected'],
                revision=candidate['revision'],
                selection_reason=reason if reason in allowed_reasons else 'frozen_selection',
                confidence_covered=(e.get('observation') is not None) if e and 'observation' in e else None,
            ),
            dict(candidate=candidate, evidence=e),
        )
        g.link(root, node, 'candidate')
        g.link(node, public_sources[candidate['submission_id']], 'frozen_source')
    opinions = g.rows(
        SecondReviewDecision.objects.filter(
            project=project, task_id_frozen__in=set(second_by_id) | {f.task_id_frozen for f in followups}
        ).exclude(pk__in=detached_opinions),
        {'receipt': (16 * 1024, 64 * 1024), 'reason': (8192, 8192)},
        lock=lock,
    )
    by_opinion = {r.task_id_frozen: r for r in opinions}
    second_keys = {}
    for row in second:
        opinion = by_opinion.get(row.pk)
        if row.status != 'completed' and opinion is not None:
            c.fail()
        if opinion:
            if opinion.task_id not in (None, row.pk) or service.digest(opinion.receipt) != opinion.receipt_hash:
                c.fail()
            second_review_service.checked_receipt(
                opinion.receipt, 'decision', row, opinion.idempotency_key, opinion.request_hash, decision=opinion
            )
        k = g.add(
            'second_review',
            row.pk,
            dict(
                state=row.status,
                effect=c.EFFECT,
                created_at=stamp(row.created_at),
                outcome=opinion.outcome if opinion else None,
                reason_state=('present_redacted' if opinion.reason else 'absent') if opinion else 'unavailable',
                actor_incarnation='not_recorded',
            ),
            dict(
                source_hash=row.source_hash,
                receipt_hash=row.receipt_hash,
                proof=source_metadata(row.proof),
                opinion_hash=opinion.receipt_hash if opinion else None,
                cancellation_hash=row.cancellation_hash,
            ),
        )
        second_keys[row.pk] = k
        source_key = public_sources.get(row.submission_id_frozen)
        if source_key is None:
            c.fail()
        g.link(source_key, k, 'supervision')
        if row.status == 'completed' and opinion is None and not any(f.task_id_frozen == row.pk for f in followups):
            missing = g.add(
                'gap',
                'opinion:' + str(row.pk),
                dict(reason='second_opinion_unavailable'),
                gap='second_opinion_unavailable',
            )
            g.link(k, missing, 'opinion_gap')
        if opinion:
            op = g.add(
                'second_opinion',
                opinion.pk,
                dict(
                    outcome=opinion.outcome,
                    effect=c.EFFECT,
                    created_at=stamp(opinion.created_at),
                    reason_state='present_redacted' if opinion.reason else 'absent',
                    actor_incarnation='not_recorded',
                ),
                dict(
                    receipt_hash=opinion.receipt_hash,
                    actor_occurrence=[str(opinion.pk), 'reviewer', opinion.actor_id_frozen],
                ),
            )
            g.link(k, op, 'opinion')
        if row.status == 'cancelled':
            ack = row.cancellation['receipt']
            cancel = g.add(
                'cancellation',
                'second:' + str(row.pk),
                dict(
                    created_at=ack['created_at'],
                    effect=c.EFFECT,
                    reason_state='present_redacted' if ack['reason'] else 'absent',
                    actor_incarnation='not_recorded',
                ),
                dict(receipt_hash=row.cancellation_hash, owner=ack['owner'], key=str(row.cancel_key)),
            )
            g.link(k, cancel, 'cancellation')
    for row in followups:
        source = row.origin_ref['source']
        if row.second_review_task_id is not None:
            live = second_by_id.get(row.second_review_task_id)
            if live is None or service.digest(live.proof) != service.digest(source):
                c.fail()
        if row.opinion_id is not None:
            live_opinion = by_opinion.get(row.task_id_frozen)
            frozen = row.origin_ref['opinion']
            if (
                live_opinion is None
                or live_opinion.pk != row.opinion_id
                or live_opinion.receipt_hash != row.origin_ref['opinion_receipt_hash']
                or service.digest(live_opinion.receipt) != live_opinion.receipt_hash
                or c.exact(live_opinion.receipt) != c.exact(frozen)
                or live_opinion.outcome != frozen['outcome']
                or live_opinion.reason != frozen['reason']
                or stamp(live_opinion.created_at) != frozen['created_at']
            ):
                c.fail()
        parent = second_keys.get(row.task_id_frozen)
        if parent is None:
            parent = g.add(
                'second_review',
                row.task_id_frozen,
                dict(state='unavailable', effect=c.EFFECT, evidence='frozen_receipt_verified'),
                dict(source_hash=service.digest(source), proof=source_metadata(source)),
                gap='live_origin_unavailable',
            )
            second_keys[row.task_id_frozen] = parent
            g.link(public_sources[source['subject']['submission_id']], parent, 'retained_supervision')
        k = g.add(
            'concern_followup',
            row.pk,
            dict(
                disposition=row.disposition,
                effect=c.EFFECT,
                created_at=stamp(row.created_at),
                reason_state='present_redacted',
                actor_incarnation='not_recorded',
            ),
            dict(
                receipt_hash=row.receipt_hash,
                origin_hash=service.digest(row.origin_ref),
                target_hash=service.digest(row.target_ref),
                actor_occurrence=[str(row.pk), 'manager', row.actor_id_frozen],
            ),
        )
        g.link(parent, k, 'followup')
        opinion_key = ('second_opinion', str(row.opinion_id_frozen))
        if opinion_key not in g.facts:
            frozen = row.origin_ref['opinion']
            opinion_key = g.add(
                'second_opinion',
                row.opinion_id_frozen,
                dict(
                    outcome='concern',
                    effect=c.EFFECT,
                    created_at=frozen['created_at'],
                    reason_state='present_redacted',
                    actor_incarnation='not_recorded',
                    evidence='observed_at_capture' if row.opinion_id is not None else 'frozen_receipt_verified',
                ),
                dict(
                    receipt_hash=row.origin_ref['opinion_receipt_hash'],
                    actor_occurrence=[str(row.opinion_id_frozen), 'reviewer', frozen['owner']['actor_id']],
                ),
                gap='live_origin_unavailable' if row.opinion_id is None else None,
            )
            g.link(parent, opinion_key, 'retained_opinion')
        g.link(opinion_key, k, 'concern_disposition')
        target = public_sources.get(row.related_submission_id) if row.related_submission_id is not None else None
        if target is not None:
            observed = g.facts[target]['bindings']['pins']
            expected = row.target_ref['subject']
            if any(
                observed[key] != value for key, value in expected.items() if key not in ('assignee_id', 'submitter_id')
            ) or any(
                observed[key] is not None and observed[key] != expected[key] for key in ('assignee_id', 'submitter_id')
            ):
                c.fail()
        if target is None and row.target_ref:
            pins = row.target_ref['subject']
            type_proof = config_proofs.get((pins['assignment_id'], pins['label_config_sha256']))
            if type_proof is None:
                c.fail('INPUT_SCOPE_UNPROVABLE', 422)
            target = g.add(
                'revision',
                pins['submission_id'],
                dict(revision=pins['revision'], state='unavailable', evidence='frozen_receipt_verified'),
                dict(pins=pins, target_hash=service.digest(row.target_ref), type_proof=type_proof),
                gap='live_origin_unavailable',
            )
            public_sources[pins['submission_id']] = target
            g.link(root, target, 'retained_root_assignment_revision')
        if target:
            g.link(k, target, 'related_revision')
    cohort = 0
    for row in links:
        handoff.checked(row, project)
        if row.review_batch_id != batch.pk:
            c.fail('INPUT_SCOPE_UNPROVABLE', 422)
        frozen_candidate = next((v for v in candidates if v['submission_id'] == row.submission_id_frozen), None)
        if (
            row.origin['review']['plan_hash'] != batch.plan_hash
            or frozen_candidate is None
            or not frozen_candidate['selected']
            or row.origin['review']['candidate'] != {k: v for k, v in frozen_candidate.items() if k != 'selected'}
        ):
            c.fail()
        target = by_target.get(row.arbitration_id) if row.arbitration_id is not None else None
        if row.arbitration_id is not None and target is None:
            c.fail('INPUT_SCOPE_UNPROVABLE', 422)
        decision = by_decision.get(row.arbitration_id_frozen)
        if target:
            arbitration.checked(target, project)
            if (
                target.created_at.isoformat() != row.origin['target']['created_at']
                or target.origin_hash != row.origin['target']['origin_hash']
                or target.input_set_hash != row.origin['target']['input_set_hash']
            ):
                c.fail()
            if decision:
                arbitration.checked_decision(decision, target)
            if (target.status == 'resolved') != (decision is not None):
                c.fail()
        target_key = ('arbitration', str(row.arbitration_id_frozen))
        if target_key not in g.facts:
            target_key = g.add(
                'arbitration',
                row.arbitration_id_frozen,
                dict(
                    state=target.status if target else 'unavailable',
                    rule='adopt-exact-submission/v1',
                    reason_state=('present_redacted' if decision.reason else 'absent') if decision else 'unavailable',
                ),
                dict(target=row.origin['target'], decision_hash=decision.payload_hash if decision else None),
                gap=None if target else 'live_origin_unavailable',
            )
        k = g.add(
            'handoff',
            row.pk,
            dict(mode=row.origin['mode'], created_at=stamp(row.created_at)),
            dict(origin_hash=row.origin_hash),
        )
        g.link(root, k, 'direct_handoff')
        g.link(k, target_key, 'arbitration')
        if decision:
            decision_key = ('arbitration_decision', str(decision.pk))
            if decision_key not in g.facts:
                decision_key = g.add(
                    'arbitration_decision',
                    decision.pk,
                    dict(
                        decision='adopt_exact_submission',
                        rule='adopt-exact-submission/v1',
                        created_at=stamp(decision.created_at),
                        reason_state='present_redacted' if decision.reason else 'absent',
                        actor_incarnation='not_recorded',
                    ),
                    dict(
                        winner=decision.winner,
                        payload_hash=decision.payload_hash,
                        actor_occurrence=[decision.pk, 'arbitrator', decision.actor_id_frozen],
                    ),
                )
                g.link(target_key, decision_key, 'authoritative_decision')
        if target and target.status == 'cancelled':
            cancel_key = ('cancellation', 'arbitration:' + str(target.pk))
            if cancel_key not in g.facts:
                cancellation = target.cancellation
                cancel = g.add(
                    *cancel_key,
                    dict(
                        created_at=cancellation['created_at'],
                        reason_state='present_redacted' if cancellation.get('reason') else 'absent',
                        actor_incarnation='not_recorded',
                    ),
                    dict(
                        payload_hash=cancellation['payload_hash'],
                        actor_occurrence=[str(target.pk), 'canceller', cancellation['actor_id']],
                    ),
                )
                g.link(target_key, cancel, 'cancellation')
        if target:
            for item in target.origin.get('item', {}).get('cohort', []):
                boundary = ('boundary_source_ref', f'{target.pk}:{service.digest(item)}')
                if boundary not in g.facts:
                    cohort += 1
                    if cohort > c.limits()['cohort']:
                        c.fail('TOO_LARGE', 422)
                    boundary = g.add(*boundary, dict(evidence='frozen_receipt_verified'), dict(cohort=item))
                g.link(target_key, boundary, 'cohort_boundary')
    differences(g, sources)
    return batch, g


def differences(graph, sources):
    """Adjacent known revisions include explicit unavailable comparisons."""
    live = {row.pk: row for row in sources}
    revisions = []
    for (kind, key), fact in list(graph.facts.items()):
        if kind != 'revision':
            continue
        pins = fact['bindings'].get('pins')
        if pins is None:
            pins = fact['bindings']['proofs'][0]['subject']
        revisions.append((pins['assignment_id'], fact['public']['revision'], int(key)))
    if len({(assignment, revision) for assignment, revision, _ in revisions}) != len(revisions):
        c.fail()
    previous = {}
    for assignment, revision, source_id in sorted(revisions):
        older = previous.get(assignment)
        if older is not None:
            old_revision, old_id = older
            before, after = live.get(old_id), live.get(source_id)
            gap = (
                'revision_sequence_gap'
                if revision != old_revision + 1
                else 'difference_input_unavailable'
                if before is None or after is None
                else None
            )
            diff = dict(state='unavailable', added_count=None, removed_count=None)
            if gap is None:
                try:
                    if (
                        before.result_snapshot['project']['label_config']
                        != after.result_snapshot['project']['label_config']
                    ):
                        raise classification.Incomparable('config_mismatch')
                    if service.digest(before.result_snapshot['task']['data']) != service.digest(
                        after.result_snapshot['task']['data']
                    ):
                        raise classification.Incomparable('task_data_mismatch')
                    schema = classification.controls(after.result_snapshot['project']['label_config'])
                    a, b = classification.answers(before, schema), classification.answers(after, schema)
                    diff = dict(
                        state='equal' if a == b else 'changed',
                        added_count=sum(len(set(b[k]) - set(a[k])) for k in a),
                        removed_count=sum(len(set(a[k]) - set(b[k])) for k in a),
                    )
                except classification.Incomparable:
                    pass
            node = graph.add(
                'difference',
                f'{old_id}:{source_id}',
                diff,
                dict(comparison_rule=classification.RULE, difference_rule='adjacent-known-revisions/v1'),
                gap=gap,
            )
            graph.link(('revision', str(old_id)), node, 'before')
            graph.link(node, ('revision', str(source_id)), 'after')
        previous[assignment] = (revision, source_id)
