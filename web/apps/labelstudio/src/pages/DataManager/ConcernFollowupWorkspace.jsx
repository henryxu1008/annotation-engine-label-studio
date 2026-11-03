import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { FrozenReviewSnapshot } from "./FrozenReviewSnapshot";
import { fieldDigest } from "./fieldEvidence";
import { parse, reviewReasonSchema, hasReviewReason } from "./secondReviewResponses";
import {
  followupList,
  followupContext,
  followupDetail,
  followupAck,
  targetRef,
  listMatches,
  contextMatches,
  detailMatches,
  followupAckMatches,
  followupDefinitiveRejection,
} from "./concernFollowupResponses";

export const ConcernFollowupEntry = ({ projectId, disabled, onOpen }) => {
  const api = useAPI();
  const { t } = useLocaleTranslation("reviewBatches");
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let active = true;
    setEnabled(false);
    Promise.resolve(
      call.current("concernFollowups", { params: { pk: projectId, page_size: 1 }, errorFilter: () => true }),
    )
      .then((raw) => {
        const r = parse(followupList, raw);
        if (active && listMatches(r, projectId)) setEnabled(true);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [projectId]);
  return enabled ? (
    <Button data-testid="open-concern-followups" disabled={disabled} onClick={onOpen}>
      {t("followupTitle")}
    </Button>
  ) : null;
};

export const ConcernFollowupWorkspace = (props) => <ScopedFollowup key={props.projectId} {...props} />;

const ScopedFollowup = ({ projectId, onBack }) => {
  const api = useAPI();
  const { t } = useLocaleTranslation("reviewBatches");
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const generation = useRef(0),
    alive = useRef(true),
    owner = useRef(null),
    intent = useRef(null);
  const heading = useRef(null),
    recoverButton = useRef(null);
  const [page, setPage] = useState(null),
    [cap, setCap] = useState(null);
  const [selected, setSelected] = useState(null),
    [cursor, setCursor] = useState(null);
  const [preview, setPreview] = useState(null),
    [detail, setDetail] = useState(null);
  const [target, setTarget] = useState(null),
    [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false),
    [busy, setBusy] = useState(false);
  const [unknown, setUnknown] = useState(false),
    [error, setError] = useState(false),
    [saved, setSaved] = useState(null);
  const clear = () => {
    setPage(null);
    setCap(null);
    setPreview(null);
    setDetail(null);
  };
  const focus = () => heading.current?.focus();
  const dto = async (name, schema, params = {}) =>
    parse(
      schema,
      await call.current(name, {
        params: { pk: projectId, ...params },
        errorFilter: () => true,
      }),
    );
  const acceptOwner = (fresh) => {
    if (!listMatches(fresh, projectId)) throw new Error("scope changed");
    if (owner.current && owner.current !== fresh.owner_hash) {
      intent.current = null;
      setUnknown(false);
      setSaved(null);
      setReason("");
      setConfirmed(false);
      owner.current = fresh.owner_hash;
      throw new Error("owner changed");
    }
    owner.current = fresh.owner_hash;
  };
  const load = async (row = selected, next = cursor, related = target) => {
    const seq = ++generation.current;
    clear();
    setBusy(true);
    setError(false);
    try {
      const fresh = await dto("concernFollowups", followupList, row ? { page_size: 1 } : next ? { cursor: next } : {});
      if (seq !== generation.current || !alive.current) return;
      acceptOwner(fresh);
      if (row?.followup_id) {
        const value = await dto("concernFollowupDetail", followupDetail, { followup: row.followup_id });
        if (!detailMatches(value, projectId, row.followup_id, fresh)) throw new Error("wrong disposition");
        if (seq !== generation.current || !alive.current) return;
        setDetail(value);
      } else if (row) {
        if (!fresh.can_record) throw new Error("current operation required");
        const value = await dto("concernFollowupContext", followupContext, {
          task: row.task_id,
          ...(related === null ? {} : { related_submission_id: related }),
        });
        if (
          !contextMatches(value, projectId, row.task_id, related, fresh) ||
          value.original.subject.submission_id !== row.submission_id
        )
          throw new Error("wrong concern context");
        if (seq !== generation.current || !alive.current) return;
        setPreview(value);
      } else setPage(fresh);
      setCap(fresh);
    } catch {
      if (seq === generation.current && alive.current) {
        clear();
        setError(true);
      }
    } finally {
      if (seq === generation.current && alive.current) setBusy(false);
    }
  };
  useLayoutEffect(() => {
    focus();
  }, []);
  useLayoutEffect(() => {
    if (unknown && !busy) recoverButton.current?.focus();
  }, [unknown, busy]);
  useEffect(() => {
    alive.current = true;
    load(null, null, null);
    const blur = () => {
      ++generation.current;
      clear();
      setBusy(false);
      setError(true);
      if (intent.current) setUnknown(true);
    };
    window.addEventListener("blur", blur);
    return () => {
      alive.current = false;
      ++generation.current;
      window.removeEventListener("blur", blur);
    };
  }, []);
  const read = (row, next, related) => {
    focus();
    load(row, next, related);
  };
  const choose = (row) => {
    setSelected(row);
    setTarget(null);
    setReason("");
    setConfirmed(false);
    setSaved(null);
    read(row, cursor, null);
  };
  const submit = async () => {
    if (busy) return;
    focus();
    const seq = ++generation.current;
    setBusy(true);
    setError(false);
    let pending = intent.current;
    try {
      const fresh = await dto("concernFollowups", followupList, { page_size: 1 });
      if (seq !== generation.current || !alive.current) return;
      acceptOwner(fresh);
      if (!fresh.can_record) throw new Error("current operation required");
      if (!pending) {
        if (
          !confirmed ||
          !hasReviewReason(reason) ||
          !preview ||
          !cap ||
          !contextMatches(preview, projectId, selected.task_id, target, fresh)
        )
          throw new Error("refresh and confirm disposition");
        pending = {
          projectId,
          owner: fresh.owner_hash,
          key: uuidv4(),
          targetHash: fieldDigest(targetRef(preview.target)),
          body: {
            second_review_task_id: selected.task_id,
            expected_concern_hash: preview.concern_hash,
            expected_context_hash: preview.context_hash,
            expected_viewer_hash: preview.viewer_hash,
            expected_version: 0,
            disposition: target === null ? "no_change" : "revision_linked",
            reason,
            related_submission_id: target,
          },
        };
        Object.freeze(pending.body);
        Object.freeze(pending);
        intent.current = pending;
      }
      if (pending.owner !== fresh.owner_hash) throw new Error("owner changed");
      let raw;
      try {
        raw = await call.current("concernFollowupCreate", {
          params: { pk: projectId },
          // The shared API proxy merges transport parameters into its body.
          // Keep the original command immutable across UNKNOWN recovery.
          body: { ...pending.body },
          headers: { "Idempotency-Key": pending.key },
          errorFilter: () => true,
        });
      } catch {
        raw = null;
      }
      if (seq !== generation.current || !alive.current) return;
      let ack;
      try {
        ack = parse(followupAck, raw);
        if (!followupAckMatches(ack, pending, fresh)) throw new Error("wrong acknowledgement");
      } catch {
        clear();
        setError(true);
        if (followupDefinitiveRejection(raw)) {
          intent.current = null;
          setUnknown(false);
        } else setUnknown(true);
        return;
      }
      const current = await dto("concernFollowups", followupList, { page_size: 1 });
      if (seq !== generation.current || !alive.current) return;
      acceptOwner(current);
      if (current.owner_hash !== pending.owner) throw new Error("owner changed");
      intent.current = null;
      setUnknown(false);
      setReason("");
      setConfirmed(false);
      setSaved({ id: ack.receipt.followup_id, owner: current.owner_hash });
      const row = { ...selected, task_id: pending.body.second_review_task_id, followup_id: ack.receipt.followup_id };
      setSelected(row);
      clear();
      await load(row, cursor, null);
    } catch {
      if (seq === generation.current && alive.current) {
        clear();
        setError(true);
        if (pending && intent.current === pending && pending.owner === owner.current) setUnknown(true);
      }
    } finally {
      if (seq === generation.current && alive.current) setBusy(false);
    }
  };
  const savedHere = Boolean(saved && cap && saved.id === selected?.followup_id && saved.owner === cap.owner_hash);
  const original = preview?.original ?? detail?.original;
  return (
    <section className="second-review" data-testid="concern-followup-workspace" aria-busy={busy}>
      <h3 ref={heading} tabIndex={-1}>
        {t("followupTitle")}
      </h3>
      <p>{t("followupBoundary")}</p>
      {savedHere && <p role="status">{t("followupSaved")}</p>}
      {error && <p role="alert">{t("followupReadFailed")}</p>}
      {unknown && <p role="status">{t("followupUnknown")}</p>}
      <nav>
        <Button data-testid="followup-refresh" disabled={busy} onClick={() => read(selected, cursor, target)}>
          {t("secondRefresh")}
        </Button>
        {selected && (
          <Button disabled={busy || unknown} data-testid="followup-back-list" onClick={() => choose(null)}>
            {t("followupBackList")}
          </Button>
        )}
        {onBack && (
          <Button disabled={busy || unknown} onClick={onBack}>
            {t("followupBack")}
          </Button>
        )}
        {unknown && (
          <Button ref={recoverButton} data-testid="followup-recover" disabled={busy} onClick={submit}>
            {t("secondRecover")}
          </Button>
        )}
      </nav>
      {page && (
        <>
          <ul>
            {page.results.map((row) => (
              <li key={row.opinion_id}>
                <Button
                  data-concern-task={row.task_id}
                  disabled={busy || unknown || (!row.followup_id && !page.can_record)}
                  onClick={() => choose(row)}
                >
                  {t("followupSource", { source: row.submission_id, revision: row.revision })} —{" "}
                  {t(row.followup_id ? "followupRecorded" : "followupUnrecorded")}
                </Button>
              </li>
            ))}
          </ul>
          {!page.results.length && <p>{t("followupEmpty")}</p>}
          <Button
            disabled={busy || unknown || !cursor}
            onClick={() => {
              setCursor(null);
              read(null, null, null);
            }}
          >
            {t("secondFirstPage")}
          </Button>
          <Button
            disabled={busy || unknown || !page.next}
            onClick={() => {
              setCursor(page.next);
              read(null, page.next, null);
            }}
          >
            {t("next")}
          </Button>
        </>
      )}
      {original && (
        <>
          <h4>{t("secondInitial")}</h4>
          <p>{t("followupSource", { source: original.subject.submission_id, revision: original.subject.revision })}</p>
          <p>
            {t(`followupState_${original.first_review.decision}`)} — {original.first_review.reason}
          </p>
          <p>
            {t("secondOutcome_concern")} — {(preview?.opinion ?? detail.original.opinion).reason}
          </p>
          {preview && <FrozenReviewSnapshot snapshot={original.result_snapshot} />}
        </>
      )}
      {preview && (
        <fieldset disabled={busy || unknown || !cap?.can_record}>
          <legend>{t("followupDisposition")}</legend>
          <label>
            {t("followupTarget")}
            <select
              data-testid="followup-target"
              value={target ?? ""}
              onChange={(e) => {
                const related = e.target.value ? Number(e.target.value) : null;
                setTarget(related);
                setConfirmed(false);
                setReason("");
                read(selected, cursor, related);
              }}
            >
              <option value="">{t("followupNoChange")}</option>
              {preview.candidates.map((v) => (
                <option key={v.id} value={v.id}>
                  {t("followupSource", { source: v.id, revision: v.revision })}
                </option>
              ))}
            </select>
          </label>
          {preview.target && (
            <>
              <p>{t("followupPreviewTargetState", { state: t(`followupState_${preview.target.status_at_record}`) })}</p>
              <FrozenReviewSnapshot snapshot={preview.target.result_snapshot} />
            </>
          )}
          <label>
            {t("followupReason")}
            <textarea
              data-testid="followup-reason"
              maxLength={4000}
              aria-describedby="followup-reason-limit"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          <label>
            <input
              data-testid="followup-confirm"
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            {t("followupConfirm")}
          </label>
          <p id="followup-reason-limit">{t("followupReasonLimit")}</p>
          <Button
            data-testid="followup-record"
            disabled={!confirmed || !hasReviewReason(reason) || !reviewReasonSchema.safeParse(reason).success}
            onClick={submit}
          >
            {t("followupRecord")}
          </Button>
        </fieldset>
      )}
      {detail && (
        <>
          <p>
            {t("followupRecorded")}:{" "}
            {t(detail.receipt.disposition === "no_change" ? "followupNoChange" : "followupRevisionLinked")}
          </p>
          <p>{detail.receipt.reason}</p>
          <p>{t("followupRecorder", { actor: detail.receipt.owner.actor_id, time: detail.receipt.created_at })}</p>
          {detail.target && (
            <p>
              {t("followupSource", {
                source: detail.target.subject.submission_id,
                revision: detail.target.subject.revision,
              })}{" "}
              — {t("followupTargetState", { state: t(`followupState_${detail.target.status_at_record}`) })}
            </p>
          )}
          <p>{t("followupHistorical")}</p>
        </>
      )}
    </section>
  );
};
