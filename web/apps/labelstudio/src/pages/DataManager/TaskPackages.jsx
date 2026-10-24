import { useCallback, useEffect, useRef, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { cn } from "../../utils/bem";
import { displayWorkState } from "./workDisplay";
import "./TaskPackages.scss";

const failed = (result) => !result || result.error || result.$meta?.ok === false;
const packages = cn("task-packages");

export const TaskPackageTitle = () => {
  const { t } = useLocaleTranslation("taskPackages");
  return t("title");
};

export const TaskPackageEntry = ({ projectId, onOpen, disabled }) => {
  const api = useAPI();
  const { t } = useLocaleTranslation("taskPackages");
  const [enabled, setEnabled] = useState(false);
  const callApi = useRef(api.callApi);
  callApi.current = api.callApi;
  useEffect(() => {
    let disposed = false;
    setEnabled(false);
    Promise.resolve(callApi.current("taskPackageCapability", { params: { pk: projectId }, errorFilter: () => true }))
      .then((result) => {
        if (!disposed) setEnabled(!failed(result) && result.enabled === true);
      })
      .catch(() => {});
    return () => {
      disposed = true;
    };
  }, [projectId]);
  return enabled ? (
    <Button size="small" look="outlined" onClick={onOpen} disabled={disabled} data-testid="open-task-packages">
      {t("title")}
    </Button>
  ) : null;
};

export const PackageCounts = ({ counts = {}, role, compact = false }) => {
  const { t } = useLocaleTranslation("taskPackages");
  const keys = !compact
    ? Object.keys(counts)
    : role === "reviewer"
      ? ["pending_review"]
      : role === "manager"
        ? [
            "fixed_tasks",
            "unassigned_tasks",
            "in_progress",
            "rework",
            "pending_review",
            "approved",
            "blocked_assignments",
            "unavailable_tasks",
          ]
        : ["assigned", "in_progress", "rework", "submitted", "approved", "conflict"];
  return (
    <dl className={packages.elem("counts").toClassName()}>
      {keys
        .filter((key) => counts[key] !== undefined)
        .map((key) => (
          <div key={key}>
            <dt>{t(`count_${key}`)}</dt>
            <dd>{counts[key]}</dd>
          </div>
        ))}
    </dl>
  );
};

const countKey = (state) =>
  ({
    unassigned: "unassigned_tasks",
    unavailable: "unavailable_tasks",
  })[state] ?? state;

export const TaskPackages = ({ projectId, initialTaskIds = [], onOpenReviewQueue, onSelectPackage }) => {
  const { t } = useLocaleTranslation("taskPackages");
  const { t: tc } = useLocaleTranslation("collaboration");
  const api = useAPI();
  const callApi = useRef(api.callApi);
  callApi.current = api.callApi;
  const generation = useRef(0);
  const [data, setData] = useState(null);
  const [selected, setSelected] = useState(null);
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState("");
  const listPage = useRef(1);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [instructions, setInstructions] = useState("");
  const [source, setSource] = useState("");
  const [ids, setIds] = useState(initialTaskIds.join(", "));
  const [preview, setPreview] = useState(null);
  const request = useRef(null);

  const load = useCallback(
    async (packageId = null, requestedPage = 1, state = "") => {
      const current = ++generation.current;
      setLoading(true);
      setData(null);
      setError("");
      setSelected(packageId);
      setPage(requestedPage);
      setFilter(state);
      try {
        const fetchPage = (value) =>
          callApi.current(packageId ? "taskPackageDetail" : "taskPackages", {
            params: {
              pk: projectId,
              ...(packageId ? { package: packageId } : {}),
              page: value,
              page_size: 50,
              ...(packageId && state ? { state } : {}),
            },
            errorFilter: () => true,
          });
        let response = await fetchPage(requestedPage);
        if (current !== generation.current) return;
        if (requestedPage > 1 && response?.response?.display_context?.reason === "PACKAGE_PAGE_OUT_OF_RANGE") {
          requestedPage = 1;
          response = await fetchPage(1);
        }
        if (current !== generation.current) return;
        if (failed(response)) setError("loadFailed");
        else {
          setData(response);
          setPage(requestedPage);
          if (!packageId) listPage.current = requestedPage;
        }
      } catch {
        if (current === generation.current) setError("loadFailed");
      } finally {
        if (current === generation.current) setLoading(false);
      }
    },
    [projectId],
  );

  useEffect(() => {
    request.current = null;
    setPreview(null);
    setSaving(false);
    setName("");
    setInstructions("");
    setSource("");
    setIds(initialTaskIds.join(", "));
    listPage.current = 1;
    load();
    return () => {
      generation.current += 1;
    };
  }, [load]);

  const makePreview = (event) => {
    event.preventDefault();
    const tokens = ids.trim().split(/[\s,，]+/);
    const taskIds = tokens.map(Number);
    if (
      !name.trim() ||
      tokens.length > 1000 ||
      tokens.some((value) => !/^[1-9]\d*$/.test(value)) ||
      taskIds.some((value) => !Number.isSafeInteger(value)) ||
      new Set(taskIds).size !== taskIds.length
    ) {
      setError("invalidInput");
      return;
    }
    setError("");
    const body = {
      name: name.trim(),
      instructions: instructions.trim(),
      source_note: source.trim(),
      task_ids: taskIds.sort((a, b) => a - b),
    };
    // Keep the same key and body after an ambiguous network failure.
    request.current = { body, key: uuidv4() };
    setPreview(body);
  };

  const create = async () => {
    if (saving || !request.current) return;
    const current = generation.current;
    setSaving(true);
    setError("");
    try {
      const response = await callApi.current("createTaskPackage", {
        params: { pk: projectId },
        body: request.current.body,
        headers: { "Idempotency-Key": request.current.key },
        errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (failed(response)) {
        const status = response?.status ?? response?.$meta?.status;
        if ([401, 403, 404].includes(status)) {
          // A revoked manager must not retain an actionable creation preview.
          setData(null);
          setPreview(null);
          request.current = null;
          setError("accessChanged");
          return;
        }
        const reason = response?.response?.display_context?.reason;
        setError(
          reason === "PACKAGE_KEY_CONFLICT"
            ? "keyConflict"
            : reason === "PACKAGE_TASKS_UNAVAILABLE"
              ? "tasksUnavailable"
              : "createFailed",
        );
      } else {
        request.current = null;
        setPreview(null);
        setName("");
        setInstructions("");
        setSource("");
        setIds("");
        setSaving(false);
        await load(response.id);
      }
    } catch {
      if (current === generation.current) setError("createFailed");
    } finally {
      if (current === generation.current) setSaving(false);
    }
  };

  return (
    <section className={packages.toClassName()} aria-label={t("title")} data-testid="task-packages">
      <p>{t("boundary")}</p>
      <div className={packages.elem("toolbar").toClassName()}>
        {selected && (
          <Button onClick={() => load(null, listPage.current)} disabled={loading || saving}>
            {t("back")}
          </Button>
        )}
        <Button onClick={() => load(selected, page, filter)} disabled={loading || saving}>
          {t("refresh")}
        </Button>
      </div>
      {loading && <p role="status">{t("loading")}</p>}
      {error && <p role="alert">{t(error)}</p>}
      {data && !selected && (
        <>
          <p>{t("overlapNotice")}</p>
          <p>{t(data.role === "manager" ? "managerScope" : data.role === "reviewer" ? "reviewScope" : "ownScope")}</p>
          {data.as_of && <p>{t("asOf", { time: data.as_of })}</p>}
          <ul>
            {data.results.map((item) => (
              <li key={item.id} data-testid={`package-summary-${item.id}`}>
                <Button disabled={saving} onClick={() => (onSelectPackage ? onSelectPackage(item) : load(item.id))}>
                  {item.name}
                </Button>
                <PackageCounts counts={item.counts} role={data.role} compact />
              </li>
            ))}
          </ul>
          {!data.results.length && <p>{t("empty")}</p>}
          {data.can_create && !onSelectPackage && (
            <form onSubmit={makePreview}>
              <h3>{t("createTitle")}</h3>
              <label>
                {t("name")}
                <input
                  value={name}
                  maxLength={160}
                  required
                  disabled={Boolean(preview)}
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
              <label>
                {t("instructions")}
                <textarea
                  value={instructions}
                  maxLength={4000}
                  disabled={Boolean(preview)}
                  onChange={(e) => setInstructions(e.target.value)}
                />
              </label>
              <label>
                {t("source")}
                <input
                  value={source}
                  maxLength={500}
                  disabled={Boolean(preview)}
                  onChange={(e) => setSource(e.target.value)}
                />
              </label>
              <label>
                {t("ids")}
                <textarea value={ids} required disabled={Boolean(preview)} onChange={(e) => setIds(e.target.value)} />
              </label>
              {!preview && <Button type="submit">{t("preview")}</Button>}
              {preview && (
                <div aria-label={t("preview")}>
                  <p>{t("previewCount", { count: preview.task_ids.length })}</p>
                  <p className={packages.elem("ids").toClassName()}>{preview.task_ids.join(", ")}</p>
                  <Button type="button" onClick={create} disabled={saving}>
                    {t(saving ? "creating" : "confirm")}
                  </Button>
                  <Button
                    type="button"
                    disabled={saving}
                    onClick={() => {
                      setPreview(null);
                      request.current = null;
                      setError("");
                    }}
                  >
                    {t("editPreview")}
                  </Button>
                </div>
              )}
            </form>
          )}
        </>
      )}
      {data && selected && (
        <>
          <h3>{data.package.name}</h3>
          <p className={packages.elem("instructions").toClassName()}>{data.package.instructions}</p>
          {data.role === "manager" && <p>{data.package.source_note}</p>}
          <p>{t(data.role === "manager" ? "managerScope" : data.role === "reviewer" ? "reviewScope" : "ownScope")}</p>
          <PackageCounts counts={data.counts} role={data.role} />
          <p>{t("asOf", { time: data.as_of })}</p>
          {data.filter_states?.length > 0 && (
            <div className={packages.elem("filters").toClassName()} role="group" aria-label={t("filterLabel")}>
              <Button
                disabled={loading}
                look={filter ? "outlined" : undefined}
                aria-pressed={!filter}
                onClick={() => load(selected, 1)}
              >
                {t("allStates")}
              </Button>
              {data.filter_states.map((state) => (
                <Button
                  key={state}
                  disabled={loading}
                  aria-pressed={filter === state}
                  look={filter === state ? undefined : "outlined"}
                  onClick={() => load(selected, 1, state)}
                >
                  {t(`count_${countKey(state)}`)}
                </Button>
              ))}
            </div>
          )}
          {filter && <p>{t("filteredRows", { count: data.count })}</p>}
          {filter && data.role === "annotator" && <p>{t("filterHint")}</p>}
          {!filter && data.continue_url && (
            <a href={data.continue_url} data-external>
              {t("continue")}
            </a>
          )}
          {data.can_review && onOpenReviewQueue && (
            <Button onClick={() => onOpenReviewQueue(data.package.id)}>{t("reviewQueue")}</Button>
          )}
          <table>
            <thead>
              <tr>
                <th>{t("task")}</th>
                <th>{t("state")}</th>
                <th>{t("action")}</th>
              </tr>
            </thead>
            <tbody>
              {data.results.map((row) => (
                <tr key={row.submission_id ?? row.assignment_id ?? row.task_id}>
                  <td>{row.task_id}</td>
                  <td>
                    {row.available === false
                      ? t("unavailable")
                      : row.states
                        ? Object.entries(row.states)
                            .map(([key, count]) => `${displayWorkState(key, tc)}: ${count}`)
                            .join("; ") || t("unassigned")
                        : displayWorkState(row.state, tc)}
                    {row.source_cancelled && ` · ${t("cancelledSource")}`}
                  </td>
                  <td>
                    {row.editor_url && (
                      <a href={row.editor_url} data-external>
                        {t("openEditor")}
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!data.results.length && <p>{t("empty")}</p>}
        </>
      )}
      {data && (
        <div className={packages.elem("toolbar").toClassName()}>
          <Button disabled={!data.previous || loading || saving} onClick={() => load(selected, page - 1, filter)}>
            {t("previous")}
          </Button>
          <span>{t("page", { page })}</span>
          <Button disabled={!data.next || loading || saving} onClick={() => load(selected, page + 1, filter)}>
            {t("next")}
          </Button>
        </div>
      )}
    </section>
  );
};
