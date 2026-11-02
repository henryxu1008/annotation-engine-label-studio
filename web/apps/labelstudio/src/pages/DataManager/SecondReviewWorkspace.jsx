import { useEffect, useRef, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { cn } from "../../utils/bem";
import { FrozenReviewSnapshot } from "./FrozenReviewSnapshot";
import { fieldDigest } from "./fieldEvidence";
import {
  ackMatches,
  ackSchema,
  assigneeSchema,
  capabilitySchema,
  contextSchema,
  definitiveRejection,
  listSchema,
  parse,
  sameOwner,
  sourceMatches,
  sourceSchema,
  taskSchema,
} from "./secondReviewResponses";
import "./SecondReviewWorkspace.scss";

export const SecondReviewEntry = ({ projectId, onOpen, disabled, onCapability }) => {
  const api = useAPI();
  const { t } = useLocaleTranslation("reviewBatches");
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const callback = useRef(onCapability);
  callback.current = onCapability;
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let disposed = false;
    setEnabled(false);
    callback.current?.(null);
    Promise.resolve(call.current("secondReviewCapability", { params: { pk: projectId }, errorFilter: () => true }))
      .then((r) => {
        const cap = parse(capabilitySchema, r);
        if (!disposed) {
          setEnabled(true);
          callback.current?.(cap);
        }
      })
      .catch(() => {});
    return () => {
      disposed = true;
    };
  }, [projectId]);
  return enabled ? (
    <Button data-testid="open-second-reviews" disabled={disabled} onClick={onOpen}>
      {t("secondTitle")}
    </Button>
  ) : null;
};

export const SecondReviewWorkspace = (props) => (
  <ScopedSecondReview key={JSON.stringify([props.projectId, props.initialSource ?? null])} {...props} />
);

const ScopedSecondReview = ({ projectId, initialSource = null, onBack }) => {
  const api = useAPI();
  const { t } = useLocaleTranslation("reviewBatches");
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const generation = useRef(0);
  const owner = useRef(null);
  const intent = useRef(null);
  const heading = useRef(null);
  const [cap, setCap] = useState(null);
  const [queue, setQueue] = useState(null);
  const [source, setSource] = useState(null);
  const [context, setContext] = useState(null);
  const [people, setPeople] = useState(null);
  const [assignee, setAssignee] = useState("");
  const [taskId, setTaskId] = useState(null);
  const [meta, setMeta] = useState(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [error, setError] = useState(false);
  const [saved, setSaved] = useState(null);
  const [cursor, setCursor] = useState(null);
  const [peopleCursor, setPeopleCursor] = useState(null);
  const clear = () => {
    setQueue(null);
    setSource(null);
    setContext(null);
    setPeople(null);
    setMeta(null);
    setCap(null);
  };
  const fetchDTO = async (method, schema, params) =>
    parse(schema, await call.current(method, { params: { pk: projectId, ...params }, errorFilter: () => true }));
  const freshCap = async (seq) => {
    const fresh = await fetchDTO("secondReviewCapability", capabilitySchema);
    if (seq !== generation.current) return null;
    if (owner.current && owner.current !== fresh.owner_hash) {
      intent.current = null;
      setUnknown(false);
      setReason("");
      setAssignee("");
      setSaved(null);
      owner.current = fresh.owner_hash;
      throw new Error("owner changed");
    }
    owner.current = fresh.owner_hash;
    return fresh;
  };
  const load = async (selected = taskId, next = cursor, nextPeople = peopleCursor) => {
    const seq = ++generation.current;
    clear();
    setBusy(true);
    setError(false);
    try {
      const fresh = await freshCap(seq);
      if (seq !== generation.current) return;
      if (!fresh.can_read) {
        setCap(fresh);
        return;
      }
      if (selected) {
        // Metadata remains readable when a historical source is unavailable.
        const record = await fetchDTO("secondReviewDetail", taskSchema, { task: selected });
        if (
          record.id !== selected ||
          record.project_id !== projectId ||
          !sameOwner(record, fresh) ||
          record.viewer_hash !== fresh.viewer_hash
        )
          throw new Error("task changed");
        let detail = null;
        try {
          detail = await fetchDTO("secondReviewSource", sourceSchema, { task: selected });
          if (!sourceMatches(detail, projectId, selected, fresh) || fieldDigest(detail.task) !== fieldDigest(record))
            throw new Error("source scope changed");
        } catch {
          detail = null;
        }
        if (seq !== generation.current) return;
        setMeta(record);
        setSource(detail);
        setCap(fresh);
      } else if (initialSource) {
        if (!fresh.can_organize) throw new Error("organization authority changed");
        const params = { batch: initialSource.batch, source: initialSource.source };
        const [preview, candidates] = await Promise.all([
          fetchDTO("secondReviewContext", contextSchema, params),
          fetchDTO("secondReviewAssignees", assigneeSchema, {
            ...params,
            ...(nextPeople ? { cursor: nextPeople } : {}),
          }),
        ]);
        if (
          [preview, candidates].some(
            (r) =>
              r.batch_id !== initialSource.batch ||
              r.submission_id !== initialSource.source ||
              !sameOwner(r, fresh) ||
              r.viewer_hash !== fresh.viewer_hash ||
              r.source_hash !== preview.source_hash,
          )
        )
          throw new Error("source changed");
        if (seq !== generation.current) return;
        setContext(preview);
        setPeople(candidates);
        setCap(fresh);
      } else {
        const page = await fetchDTO("secondReviews", listSchema, next ? { cursor: next } : {});
        if (
          !sameOwner(page, fresh) ||
          page.viewer_hash !== fresh.viewer_hash ||
          page.results.some(
            (r) => r.project_id !== projectId || !sameOwner(r, fresh) || r.viewer_hash !== fresh.viewer_hash,
          )
        )
          throw new Error("queue scope changed");
        if (seq !== generation.current) return;
        setQueue(page);
        setCap(fresh);
      }
    } catch {
      if (seq === generation.current) {
        clear();
        setError(true);
      }
    } finally {
      if (seq === generation.current) setBusy(false);
    }
  };
  useEffect(() => {
    load(null, null, null);
    heading.current?.focus();
    const blur = () => {
      ++generation.current;
      clear();
      setError(true);
      setBusy(false);
      if (intent.current) setUnknown(true);
    };
    window.addEventListener("blur", blur);
    return () => {
      ++generation.current;
      window.removeEventListener("blur", blur);
    };
  }, []);
  useEffect(() => {
    if (!busy) heading.current?.focus();
  }, [taskId, busy]);
  useEffect(() => {
    if (unknown && !busy) heading.current?.closest("section")?.querySelector('[data-testid="second-recover"]')?.focus();
  }, [unknown, busy]);

  const submit = async (command, outcome = null) => {
    if (busy || (intent.current && !unknown)) return;
    const seq = ++generation.current;
    setBusy(true);
    setError(false);
    let pending = intent.current;
    if (!pending) setSaved(null);
    try {
      const fresh = await freshCap(seq);
      if (seq !== generation.current) return;
      if (!pending) {
        if (!cap || !sameOwner(cap, fresh) || cap.viewer_hash !== fresh.viewer_hash)
          throw new Error("refresh required");
        let body;
        if (command === "dispatch") {
          if (!context?.eligible || !fresh.can_organize || !people?.results.some((u) => u.id === Number(assignee)))
            throw new Error("select an independent reviewer");
          body = {
            batch_id: initialSource.batch,
            submission_id: initialSource.source,
            expected_plan_hash: context.plan_hash,
            expected_source_hash: context.source_hash,
            assignee_id: Number(assignee),
            expected_viewer_hash: cap.viewer_hash,
          };
        } else if (command === "decision") {
          if (!source || !fresh.can_decide || meta?.status !== "pending") throw new Error("no current source");
          if (!["confirmed", "concern"].includes(outcome) || (outcome === "concern" && !reason.trim()))
            throw new Error("opinion required");
          body = {
            expected_version: meta.version,
            expected_source_hash: meta.source_hash,
            outcome,
            reason,
            expected_viewer_hash: cap.viewer_hash,
          };
        } else {
          if (!taskId || !fresh.can_cancel || !reason.trim()) throw new Error("cancellation reason required");
          body = { expected_version: meta?.version ?? 1, reason, expected_viewer_hash: cap.viewer_hash };
        }
        pending = { command, projectId, taskId: taskId ?? null, body, key: uuidv4(), owner: fresh.owner_hash };
        intent.current = pending;
      }
      if (pending.owner !== fresh.owner_hash) throw new Error("owner changed");
      const method = { dispatch: "secondReviewCreate", decision: "secondReviewDecide", cancel: "secondReviewCancel" }[
        pending.command
      ];
      let raw;
      try {
        raw = await call.current(method, {
          params: { pk: projectId, ...(pending.taskId ? { task: pending.taskId } : {}) },
          body: pending.body,
          headers: { "Idempotency-Key": pending.key },
          errorFilter: () => true,
        });
      } catch {
        raw = null;
      }
      if (seq !== generation.current) {
        if (intent.current === pending) setUnknown(true);
        return;
      }
      let ack;
      try {
        ack = parse(ackSchema, raw);
        if (!ackMatches(ack, pending, fresh)) throw new Error("wrong acknowledgement");
      } catch {
        // Only an explicit definitive rejection releases this intent. Unknown
        // transport/ACKs retain the EXACT original body/key until user recovery.
        if (definitiveRejection(raw)) {
          intent.current = null;
          setUnknown(false);
          setError(true);
          clear();
        } else {
          setUnknown(true);
          setError(true);
          clear();
        }
        return;
      }
      intent.current = null;
      setUnknown(false);
      setSaved({ taskId: ack.receipt.task_id, key: pending.key, owner: fresh.owner_hash });
      setReason("");
      setTaskId(ack.receipt.task_id);
      setBusy(false);
      await load(ack.receipt.task_id, cursor, peopleCursor);
    } catch {
      if (seq === generation.current) {
        clear();
        setError(true);
        if (pending && intent.current === pending && pending.owner === owner.current) setUnknown(true);
      }
    } finally {
      if (seq === generation.current) setBusy(false);
    }
  };

  const savedHere = saved?.taskId === taskId && saved?.owner === owner.current;
  return (
    <section className={cn("second-review").toClassName()} data-testid="second-review-workspace" aria-busy={busy}>
      <h3 ref={heading} tabIndex={-1}>
        {t("secondTitle")}
      </h3>
      <p>{t("secondBoundary")}</p>
      {savedHere && <p role="status">{t("secondSaved")}</p>}
      {error && <p role="alert">{t(savedHere ? "secondSavedReadFailed" : "secondReadFailed")}</p>}
      {unknown && <p role="status">{t("secondUnknown")}</p>}
      <nav>
        <Button disabled={busy} onClick={() => load()}>
          {t("secondRefresh")}
        </Button>
        {onBack && (
          <Button disabled={busy || unknown} onClick={onBack}>
            {t("secondBack")}
          </Button>
        )}
        {taskId && (
          <Button
            disabled={busy || unknown}
            onClick={() => {
              setTaskId(null);
              setReason("");
              setSaved(null);
              load(null);
            }}
          >
            {t(initialSource ? "secondBackSource" : "secondBackQueue")}
          </Button>
        )}
        {unknown && (
          <Button data-testid="second-recover" disabled={busy} onClick={() => submit()}>
            {t("secondRecover")}
          </Button>
        )}
      </nav>
      {queue && (
        <>
          <ul>
            {queue.results.map((row) => (
              <li key={row.id}>
                <Button
                  data-second-task={row.id}
                  disabled={busy || unknown}
                  onClick={() => {
                    setTaskId(row.id);
                    setReason("");
                    setSaved(null);
                    load(row.id);
                  }}
                >
                  #{row.submission_id} / {row.revision} — {t(`secondState_${row.status}`)}
                </Button>
              </li>
            ))}
          </ul>
          {!queue.results.length && <p>{t("secondEmpty")}</p>}
          <Button
            disabled={busy || unknown || !cursor}
            onClick={() => {
              setCursor(null);
              load(null, null);
            }}
          >
            {t("secondFirstPage")}
          </Button>
          <Button
            disabled={busy || unknown || !queue.next}
            onClick={() => {
              setCursor(queue.next);
              load(null, queue.next);
            }}
          >
            {t("next")}
          </Button>
        </>
      )}
      {context && (
        <>
          <p>{t("secondSource", { batch: context.batch_id, source: context.submission_id })}</p>
          {context.existing ? (
            <Button
              disabled={busy || unknown}
              onClick={() => {
                setTaskId(context.existing.id);
                load(context.existing.id);
              }}
            >
              {t("secondExisting")}
            </Button>
          ) : (
            <fieldset disabled={busy || unknown || !context.eligible}>
              <legend>{t("secondDispatch")}</legend>
              <label>
                {t("secondAssignee")}
                <select data-testid="second-assignee" value={assignee} onChange={(e) => setAssignee(e.target.value)}>
                  <option value="">{t("secondSelect")}</option>
                  {people?.results.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.display_name}
                    </option>
                  ))}
                </select>
              </label>
              {!people?.count && <p>{t("secondNoAssignee")}</p>}
              <Button
                disabled={!peopleCursor}
                onClick={() => {
                  setAssignee("");
                  setPeopleCursor(null);
                  load(null, cursor, null);
                }}
              >
                {t("secondFirstPage")}
              </Button>
              <Button
                disabled={!people?.next}
                onClick={() => {
                  setAssignee("");
                  setPeopleCursor(people.next);
                  load(null, cursor, people.next);
                }}
              >
                {t("next")}
              </Button>
              <Button data-testid="second-dispatch" disabled={!assignee} onClick={() => submit("dispatch")}>
                {t("secondDispatch")}
              </Button>
            </fieldset>
          )}
        </>
      )}
      {cap && !cap.can_read && cap.can_cancel && taskId && (
        <fieldset disabled={busy || unknown}>
          <legend>{t("secondCancel")}</legend>
          <p>{t("secondCancelOnly")}</p>
          <label>
            {t("secondReason")}
            <textarea
              data-testid="second-reason"
              maxLength={2000}
              rows={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          <Button data-testid="second-cancel" disabled={!reason.trim()} onClick={() => submit("cancel")}>
            {t("secondCancel")}
          </Button>
        </fieldset>
      )}
      {meta && (
        <>
          <p>
            {t("secondSource", { batch: meta.batch_id, source: meta.submission_id })} / {meta.revision}
          </p>
          <p>{t(`secondState_${meta.status}`)}</p>
          <p>{t("secondMetadata")}</p>
          {source ? (
            <>
              <p>{t(source.historical_revision ? "secondHistorical" : "secondExact")}</p>
              <h4>
                {t("secondInitial")}: {t(source.first_review.decision)}
              </h4>
              <p>{source.first_review.reason}</p>
            </>
          ) : (
            <p role="status">{t("secondUnavailable")}</p>
          )}
          <h4>{t("secondOpinion")}</h4>
          {meta.decision ? (
            <>
              <p>{t(`secondOutcome_${meta.decision.outcome}`)}</p>
              <p>{meta.decision.reason}</p>
            </>
          ) : (
            <p>{t(meta.status === "cancelled" ? "secondState_cancelled" : "secondState_pending")}</p>
          )}
          {meta.cancellation && <p>{meta.cancellation.reason}</p>}
          {source && <FrozenReviewSnapshot snapshot={source.result_snapshot} />}
          {meta.status === "pending" && cap && ((cap.can_decide && source) || cap.can_cancel) && (
            <fieldset disabled={busy || unknown}>
              <legend>{t(cap.can_decide ? "secondOpinion" : "secondCancel")}</legend>
              <label>
                {t("secondReason")}
                <textarea
                  data-testid="second-reason"
                  maxLength={2000}
                  rows={3}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              {cap.can_decide && source && (
                <>
                  <Button data-testid="second-confirm" onClick={() => submit("decision", "confirmed")}>
                    {t("secondConfirm")}
                  </Button>
                  <Button
                    data-testid="second-concern"
                    disabled={!reason.trim()}
                    onClick={() => submit("decision", "concern")}
                  >
                    {t("secondConcern")}
                  </Button>
                </>
              )}
              {cap.can_cancel && (
                <Button data-testid="second-cancel" disabled={!reason.trim()} onClick={() => submit("cancel")}>
                  {t("secondCancel")}
                </Button>
              )}
            </fieldset>
          )}
        </>
      )}
    </section>
  );
};
