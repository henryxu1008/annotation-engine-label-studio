import { useEffect, useRef, useState } from "react";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { NerArbitrations } from "./NerArbitrations";
import { nerReviewHandoff } from "./nerReviewHandoffResponses";
import { ClassificationArbitrations } from "./ClassificationArbitrations";
import { classificationReviewHandoff } from "./reviewHandoffResponses";

export const ReviewArbitrationHandoff = ({ projectId, source, onBack }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI();
  const flow = source.kind === "ner" ? nerReviewHandoff : classificationReviewHandoff;
  const Workspace = source.kind === "ner" ? NerArbitrations : ClassificationArbitrations;
  const call = useRef(api.callApi); call.current = api.callApi;
  const generation = useRef(0), heading = useRef(null);
  const [rows, setRows] = useState(null), [canCreate, setCanCreate] = useState(false), [error, setError] = useState(false);
  const [busy, setBusy] = useState(false), [page, setPage] = useState(1), [target, setTarget] = useState(null);
  const clear = () => { setRows(null); setCanCreate(false); };
  const load = async (nextPage = page) => {
    const current = ++generation.current; clear(); setBusy(true); setError(false); setPage(nextPage);
    try {
      const [raw, cap] = await Promise.all([
        call.current(flow.listMethod, { params: { pk: projectId, batch: source.batch, source: source.source, page: nextPage }, errorFilter: () => true }),
        call.current(flow.capabilityMethod, { params: { pk: projectId }, errorFilter: () => true }),
      ]);
      const list = flow.listSchema.parse(raw);
      flow.parseCapability(cap);
      if (raw?.$meta?.ok === false || cap?.$meta?.ok === false || cap?.enabled !== true || cap.can_read !== true
        || !flow.scopeMatches(list, source, projectId) || cap.viewer_hash !== list.viewer_hash
        || list.results.some((r) => !flow.scopeMatches(r, source, projectId) || r.viewer_hash !== list.viewer_hash)) throw new Error("scope changed");
      if (current === generation.current) { setRows(list); setCanCreate(cap.can_create === true && source.pending); }
    } catch { if (current === generation.current) { clear(); setError(true); } }
    finally { if (current === generation.current) setBusy(false); }
  };
  useEffect(() => {
    setTarget(null); load(1); heading.current?.focus();
    const blur = () => { ++generation.current; clear(); setBusy(false); setError(true); };
    window.addEventListener("blur", blur);
    return () => { ++generation.current; window.removeEventListener("blur", blur); };
  }, [projectId, source.batch, source.source, source.kind]);
  useEffect(() => { if (!target && !busy) heading.current?.focus(); }, [target, busy]);
  if (target) return <Workspace projectId={projectId} handoffSource={source}
    initialHandoffId={target.handoff ?? null} initialTaskId={target.arbitration ?? null}
    onBackToReview={() => { setTarget(null); load(page); heading.current?.focus(); }} />;
  return <section data-testid="review-handoff" aria-busy={busy}>
    <h3 ref={heading} tabIndex={-1}>{t(flow.title)}</h3><p>{t(flow.boundary)}</p>
    {flow.kind === 'ner' && <p>{t('nerHandoffIntegrity')}</p>}
    <p>{t("handoffSource", { batch: source.batch, source: source.source })}</p>
    {error && <p role="alert">{t("arbitrationReadFailed")}</p>}
    <Button disabled={busy} onClick={() => load()}>{t("batchRefresh")}</Button>
    <Button disabled={busy} onClick={onBack}>{t("handoffBack")}</Button>
    {canCreate && <Button data-testid="handoff-start" onClick={() => { ++generation.current; clear(); setTarget({}); }}>{t("handoffStart")}</Button>}
    {rows && <><ul>{rows.results.map((r) => <li key={r.id}>
      <Button data-handoff={r.id} onClick={() => { ++generation.current; clear(); setTarget({ handoff: r.id, arbitration: r.arbitration_id }); }}>
        #{r.arbitration_id} — {t(r.arbitration_status ? `arbitrationState_${r.arbitration_status}` : "arbitrationUnavailable")}</Button>
      <p>{t("handoffReviewState")}: {t(r.source_status === "unavailable" ? "unknownStatus" : r.source_status)}</p>
    </li>)}</ul>
      {!rows.results.length && <p>{t("handoffEmpty")}</p>}
      <Button disabled={busy || !rows.previous} onClick={() => load(page - 1)}>{t("batchPrevious")}</Button>
      <Button disabled={busy || !rows.next} onClick={() => load(page + 1)}>{t("batchNext")}</Button>
    </>}
  </section>;
};
