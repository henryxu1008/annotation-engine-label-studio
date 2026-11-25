import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";

const id = z.number().int().positive().safe();
const hash = z.string().regex(/^[a-f0-9]{64}$/);
const pageSchema = z.object({
  items: z.array(z.object({ id, submission_id: id, created_at: z.string(), manifest_hash: hash })).max(100),
  has_more: z.boolean(), next_before: id.nullable(),
}).refine((value) => (value.has_more ? value.items.length > 0 && value.next_before === value.items[value.items.length - 1].id : value.next_before === null)
  && value.items.every((item, index) => index === 0 || value.items[index - 1].id > item.id));
const detailSchema = z.object({ id, manifest_hash: hash, manifest: z.object({
  schema: z.literal("classification-blind-score/v1"), project_id: id, submission_id: id, revision: id,
  result: z.object({ status: z.literal("scored"), matched_controls: z.number().int().nonnegative(),
    total_controls: z.number().int().positive(), controls: z.array(z.unknown()) }),
}) });
const failed = (result) => !result || result.error || result.$meta?.ok === false || typeof result.status === "number";

export const ClassificationBlindScores = ({ projectId }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI();
  const call = useRef(api.callApi); call.current = api.callApi;
  const generation = useRef(0);
  const [rows, setRows] = useState([]), [before, setBefore] = useState(null), [detail, setDetail] = useState(null);
  const [loaded, setLoaded] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  useEffect(() => {
    generation.current += 1; setRows([]); setBefore(null); setDetail(null); setLoaded(false); setBusy(false); setError("");
    return () => { generation.current += 1; };
  }, [projectId]);
  const load = async (cursor = null) => {
    const epoch = generation.current; setBusy(true); setError(""); setDetail(null);
    try {
      const result = await call.current("classificationBlindScores", { params: { pk: projectId, ...(cursor ? { before: cursor } : {}) }, errorFilter: () => true });
      if (failed(result)) throw new Error("unavailable");
      const page = pageSchema.parse(result);
      if (cursor && page.items.some((item) => item.id >= cursor)) throw new Error("scope");
      if (generation.current !== epoch) return;
      setRows((old) => cursor ? [...old, ...page.items] : page.items); setBefore(page.next_before); setLoaded(true);
    } catch {
      if (generation.current === epoch) { setRows([]); setBefore(null); setLoaded(false); setError(t("goldError")); }
    } finally { if (generation.current === epoch) setBusy(false); }
  };
  const open = async (row) => {
    const epoch = generation.current; setBusy(true); setError(""); setDetail(null);
    try {
      const result = await call.current("classificationBlindScoreDetail", { params: { pk: projectId, score: row.id }, errorFilter: () => true });
      if (failed(result)) throw new Error("unavailable");
      const value = detailSchema.parse(result);
      if (value.id !== row.id || value.manifest.project_id !== projectId || value.manifest.submission_id !== row.submission_id
          || value.manifest_hash !== row.manifest_hash || value.manifest.result.matched_controls > value.manifest.result.total_controls) throw new Error("scope");
      if (generation.current === epoch) setDetail(value);
    } catch {
      if (generation.current === epoch) { setRows([]); setBefore(null); setLoaded(false); setError(t("goldError")); }
    } finally { if (generation.current === epoch) setBusy(false); }
  };
  return <section data-testid="classification-blind-scores">
    <h4>{t("blindScoresTitle")}</h4>
    <Button disabled={busy} onClick={() => load()}>{t("blindScoresLoad")}</Button>
    {error && <p role="alert">{error}</p>}
    {loaded && rows.length === 0 && <p>{t("blindScoresEmpty")}</p>}
    <ul>{rows.map((row) => <li key={row.id}><button type="button" disabled={busy} onClick={() => open(row)}>
      {t("blindScoreItem", { id: row.id })}</button> · {t("blindScoreSubmission", { id: row.submission_id })} · {row.created_at}</li>)}</ul>
    {before && <Button disabled={busy} onClick={() => load(before)}>{t("blindScoresMore")}</Button>}
    {detail && <div><p>{t("blindScoreResult", { matched: detail.manifest.result.matched_controls, total: detail.manifest.result.total_controls })}</p>
      <pre>{JSON.stringify(detail.manifest.result.controls, null, 2)}</pre></div>}
  </section>;
};
