import { useEffect, useRef, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";

const failed = (result) => !result || result.error || result.$meta?.ok === false;
const denied = (result) => [401, 403, 404].includes(result?.status ?? result?.$meta?.status);
const parseIds = (text) => {
  const tokens = text.trim().split(/[\s,，]+/);
  const ids = tokens.map(Number);
  return tokens.length <= 100 &&
    tokens.every((token) => /^[1-9]\d*$/.test(token)) &&
    ids.every(Number.isSafeInteger) &&
    new Set(ids).size === ids.length
    ? ids
    : null;
};
const localDate = (value) => {
  if (!value) return "";
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};

const MemberChooser = ({ projectId, selected, onChange, disabled, single = false }) => {
  const { t } = useLocaleTranslation("taskPackages");
  const { callApi } = useAPI();
  const api = useRef(callApi);
  api.current = callApi;
  const generation = useRef(0);
  const [page, setPage] = useState(1);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const current = ++generation.current;
    setResult(null);
    setError(false);
    Promise.resolve(
      api.current("projectMembers", {
        params: { pk: projectId, limit: 50, offset: (page - 1) * 50 },
        errorFilter: () => true,
      }),
    )
      .then((response) => {
        if (current !== generation.current) return;
        if (failed(response) || !Array.isArray(response.results)) setError(true);
        else setResult(response);
      })
      .catch(() => {
        if (current === generation.current) setError(true);
      });
    return () => {
      generation.current += 1;
    };
  }, [projectId, page]);
  const chosen = new Set(parseIds(selected) ?? []);
  const invalid = Boolean(selected.trim()) && parseIds(selected) === null;
  return (
    <fieldset disabled={disabled || invalid}>
      <legend>{t("chooseMembers")}</legend>
      {error && <p role="alert">{t("membersLoadFailed")}</p>}
      {result && (
        <>
          {result.results
            .filter((item) => item.enabled && ["manager", "annotator"].includes(item.role) && item.user?.id)
            .map((item) => (
              <label key={item.user.id}>
                <input
                  type="checkbox"
                  checked={chosen.has(item.user.id)}
                  onChange={(event) => {
                    const next = new Set(single ? [] : chosen);
                    if (event.target.checked) next.add(item.user.id);
                    else next.delete(item.user.id);
                    if (next.size > 100) return;
                    onChange([...next].sort((a, b) => a - b).join(", "));
                  }}
                />
                {item.user.email || item.user.id}
              </label>
            ))}
          <Button type="button" disabled={!result.previous} onClick={() => setPage(page - 1)}>
            {t("previous")}
          </Button>
          <span>{t("page", { page })}</span>
          <Button type="button" disabled={!result.next} onClick={() => setPage(page + 1)}>
            {t("next")}
          </Button>
        </>
      )}
    </fieldset>
  );
};

export const PackageManagement = ({ projectId, packageId, canDispatch, canManagePool, onChanged }) => {
  const { t } = useLocaleTranslation("taskPackages");
  const { callApi } = useAPI();
  const api = useRef(callApi);
  api.current = callApi;
  const [opened, setOpened] = useState(false);
  const [pool, setPool] = useState(null);
  const [candidates, setCandidates] = useState("");
  const [deadline, setDeadline] = useState("");
  const [deadlineDirty, setDeadlineDirty] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [priority, setPriority] = useState(0);
  const [automatic, setAutomatic] = useState(false);
  const [assignee, setAssignee] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const dispatchRequest = useRef(null);
  const reopenRequest = useRef(null);
  const balanceRequest = useRef(null);
  const generation = useRef(0);
  useEffect(
    () => () => {
      generation.current += 1;
    },
    [projectId, packageId],
  );

  const open = async () => {
    setOpened(true);
    if (!canManagePool) return;
    const current = ++generation.current;
    setSaving(true);
    setError("");
    try {
      const response = await api.current("taskPackagePool", {
        params: { pk: projectId, package: packageId },
        errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (failed(response)) setError("poolLoadFailed");
      else {
        setPool(response);
        setCandidates(response.candidate_ids.join(", "));
        setDeadline(localDate(response.due_at));
        setDeadlineDirty(false);
        setEnabled(response.enabled);
        setPriority(response.priority ?? 0);
        setAutomatic(response.automatic === true);
      }
    } catch {
      if (current === generation.current) setError("poolLoadFailed");
    } finally {
      if (current === generation.current) setSaving(false);
    }
  };
  const save = async (event) => {
    event.preventDefault();
    const ids = parseIds(candidates);
    if (!ids || !pool || (deadline && Number.isNaN(new Date(deadline).getTime()))) {
      setError("poolInvalid");
      return;
    }
    const current = generation.current;
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const response = await api.current("updateTaskPackagePool", {
        params: { pk: projectId, package: packageId },
        body: {
          expected_revision: pool.revision,
          candidate_ids: ids,
          enabled,
          ...(pool.advanced_enabled ? { priority: Number(priority) } : {}),
          ...(pool.automation_available || pool.automatic ? { automatic } : {}),
          due_at: deadlineDirty ? (deadline ? new Date(deadline).toISOString() : null) : pool.due_at,
        },
        errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (failed(response)) {
        if (denied(response)) setPool(null);
        setError("poolSaveFailed");
      } else {
        setPool({
          ...response,
          claimed: pool.claimed,
          claim_id: pool.claim_id,
          advanced_enabled: pool.advanced_enabled,
          automation_available: pool.automation_available,
        });
        setDeadlineDirty(false);
        setMessage("poolSaved");
      }
    } catch {
      if (current === generation.current) setError("poolSaveFailed");
    } finally {
      if (current === generation.current) setSaving(false);
    }
  };
  const dispatch = async (event) => {
    event.preventDefault();
    const id = Number(assignee);
    if (!/^[1-9]\d*$/.test(assignee) || !Number.isSafeInteger(id)) {
      setError("poolInvalid");
      return;
    }
    if (!dispatchRequest.current) dispatchRequest.current = { id, key: uuidv4() };
    const current = generation.current;
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const response = await api.current("taskPackageDispatch", {
        params: { pk: projectId, package: packageId },
        body: { assignee_id: dispatchRequest.current.id },
        headers: { "Idempotency-Key": dispatchRequest.current.key },
        errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (failed(response)) {
        if (denied(response)) {
          setPool(null);
          setOpened(false);
          dispatchRequest.current = null;
        }
        if ([400, 409].includes(response?.status ?? response?.$meta?.status)) dispatchRequest.current = null;
        setError("dispatchFailed");
      } else {
        dispatchRequest.current = null;
        setAssignee("");
        setMessage("dispatchSaved");
        onChanged();
      }
    } catch {
      if (current === generation.current) setError("dispatchFailed");
    } finally {
      if (current === generation.current) setSaving(false);
    }
  };
  const operation = async (kind) => {
    if (saving || !pool) return;
    const ref = kind === "reopen" ? reopenRequest : balanceRequest;
    if (!ref.current)
      ref.current = {
        key: uuidv4(),
        body: kind === "reopen" ? { expected_revision: pool.revision, claim_id: pool.claim_id } : {},
      };
    const current = generation.current;
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const response = await api.current(kind === "reopen" ? "reopenTaskPackage" : "balanceTaskPackage", {
        params: { pk: projectId, package: packageId },
        body: ref.current.body,
        headers: { "Idempotency-Key": ref.current.key },
        errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (failed(response)) {
        if ([400, 403, 404, 409].includes(response?.status ?? response?.$meta?.status)) ref.current = null;
        setError(kind === "reopen" ? "reopenFailed" : "balanceFailed");
      } else {
        ref.current = null;
        onChanged();
      }
    } catch {
      if (current === generation.current) setError(kind === "reopen" ? "reopenFailed" : "balanceFailed");
    } finally {
      if (current === generation.current) setSaving(false);
    }
  };
  return (
    <section aria-label={t("managementTitle")}>
      {error && <p role="alert">{t(error)}</p>}
      {!opened ? (
        <Button onClick={open}>{t("managementOpen")}</Button>
      ) : (
        <>
          <h4>{t("managementTitle")}</h4>
          {message && <p role="status">{t(message)}</p>}
          {canDispatch && (
            <form onSubmit={dispatch}>
              <label>
                {t("dispatchAssignee")}
                <input
                  value={assignee}
                  onChange={(e) => setAssignee(e.target.value)}
                  disabled={saving || Boolean(dispatchRequest.current)}
                  inputMode="numeric"
                />
              </label>
              <MemberChooser
                projectId={projectId}
                selected={assignee}
                onChange={setAssignee}
                disabled={saving || Boolean(dispatchRequest.current)}
                single
              />
              <Button type="submit" disabled={saving}>
                {t("dispatchSubmit")}
              </Button>
            </form>
          )}
          {canManagePool && pool && (
            <form onSubmit={save}>
              <h4>{t("poolTitle")}</h4>
              <label>
                {t("poolCandidates")}
                <input
                  value={candidates}
                  onChange={(e) => setCandidates(e.target.value)}
                  disabled={saving || pool.claimed}
                  required
                />
              </label>
              <MemberChooser
                projectId={projectId}
                selected={candidates}
                onChange={setCandidates}
                disabled={saving || pool.claimed}
              />
              <label>
                {t("poolDeadline")}
                <input
                  type="datetime-local"
                  value={deadline}
                  onChange={(e) => {
                    setDeadline(e.target.value);
                    setDeadlineDirty(true);
                  }}
                  disabled={saving}
                />
              </label>
              {pool.advanced_enabled && (
                <label>
                  {t("poolPriority")}
                  <input
                    type="number"
                    min="0"
                    max="100"
                    value={priority}
                    onChange={(e) => setPriority(e.target.value)}
                    disabled={saving}
                  />
                </label>
              )}
              {(pool.automation_available || pool.automatic) && (
                <label>
                  <input
                    type="checkbox"
                    checked={automatic}
                    onChange={(e) => setAutomatic(e.target.checked)}
                    disabled={saving || (!pool.automation_available && !automatic)}
                  />
                  {t("poolAutomatic")}
                </label>
              )}
              {(pool.automation_available || pool.automatic) && <p>{t("poolAutomaticHelp")}</p>}
              <label>
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                  disabled={saving}
                />
                {t("poolEnabled")}
              </label>
              <p>{t("poolDeadlineHelp")}</p>
              {pool.overdue_count > 0 && <p>{t("poolOverdue", { count: pool.overdue_count })}</p>}
              <Button type="submit" disabled={saving}>
                {t("poolSave")}
              </Button>
              {pool.advanced_enabled && pool.claimed && (
                <Button type="button" disabled={saving} onClick={() => operation("reopen")}>
                  {t("poolReopen")}
                </Button>
              )}
              {pool.advanced_enabled && !pool.claimed && canDispatch && (
                <Button type="button" disabled={saving} onClick={() => operation("balance")}>
                  {t("poolBalance")}
                </Button>
              )}
            </form>
          )}
          <Button disabled={saving} onClick={open}>
            {t("poolReload")}
          </Button>
        </>
      )}
    </section>
  );
};

export const PackagePoolList = ({ projectId, onClaimed }) => {
  const { t } = useLocaleTranslation("taskPackages");
  const { callApi } = useAPI();
  const api = useRef(callApi);
  api.current = callApi;
  const generation = useRef(0);
  const requests = useRef({});
  const [data, setData] = useState(null);
  const [notices, setNotices] = useState([]);
  const [noticeAfter, setNoticeAfter] = useState(null);
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = async (number = 1) => {
    const current = ++generation.current;
    setBusy(true);
    setError("");
    setData(null);
    setNotices([]);
    setNoticeAfter(null);
    try {
      const [response, alerts] = await Promise.all([
        api.current("taskPackagePools", {
          params: { pk: projectId, page: number, page_size: 50 },
          errorFilter: () => true,
        }),
        api.current("reconcilePackageOverdueNotices", { params: { pk: projectId }, body: {}, errorFilter: () => true }),
      ]);
      if (current !== generation.current) return;
      if (denied(alerts)) {
        setData(null);
        setNotices([]);
        setError("noticeFailed");
        return;
      }
      if (failed(alerts) || !Array.isArray(alerts.results)) {
        setNotices([]);
        setError("noticeFailed");
      } else {
        setNotices(alerts.results);
        setNoticeAfter(alerts.next_after);
      }
      if (failed(response)) {
        if (denied(response)) {
          setNotices([]);
          setNoticeAfter(null);
        }
        setError("poolLoadFailed");
      } else {
        setData(response);
        setPage(number);
      }
    } catch {
      if (current === generation.current) setError("poolLoadFailed");
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };
  useEffect(() => {
    requests.current = {};
    load();
    return () => {
      generation.current += 1;
    };
  }, [projectId]);
  const loadMoreNotices = async () => {
    if (!noticeAfter || busy) return;
    const current = generation.current;
    setBusy(true);
    setError("");
    try {
      const response = await api.current("reconcilePackageOverdueNotices", {
        params: { pk: projectId, after: noticeAfter },
        body: {},
        errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (failed(response) || !Array.isArray(response.results)) {
        if (denied(response)) {
          setNotices([]);
          setNoticeAfter(null);
          setData(null);
        }
        setError("noticeFailed");
      } else {
        setNotices((old) => [...old, ...response.results]);
        setNoticeAfter(response.next_after);
      }
    } catch {
      if (current === generation.current) setError("noticeFailed");
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };
  const claim = async (id) => {
    const key = requests.current[id] ?? uuidv4();
    requests.current[id] = key;
    const current = generation.current;
    setBusy(true);
    setError("");
    try {
      const response = await api.current("claimTaskPackage", {
        params: { pk: projectId, package: id },
        body: {},
        headers: { "Idempotency-Key": key },
        errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (failed(response)) {
        if (denied(response)) {
          setData(null);
          setNotices([]);
          setNoticeAfter(null);
        }
        setError("claimFailed");
      } else if (response.released) {
        delete requests.current[id];
        setError("claimReleased");
      } else {
        delete requests.current[id];
        onClaimed(id);
      }
    } catch {
      if (current === generation.current) setError("claimFailed");
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };
  return (
    <section aria-label={t("availablePools")}>
      <h4>{t("availablePools")}</h4>
      {error && <p role="alert">{t(error)}</p>}
      {notices.length > 0 && (
        <div role="status">
          <h4>{t("overdueNotices")}</h4>
          {notices.map((notice) => (
            <p key={notice.id}>
              {notice.name}: {t("poolOverdue", { count: notice.overdue_count })}
            </p>
          ))}
        </div>
      )}
      {noticeAfter && (
        <Button disabled={busy} onClick={loadMoreNotices}>
          {t("moreNotices")}
        </Button>
      )}
      {data && (
        <>
          <ul>
            {data.results.map((item) => (
              <li key={item.package_id}>
                <span>{item.name}</span> <span>{t("poolPriorityValue", { priority: item.priority ?? 0 })}</span>{" "}
                <span>
                  {item.due_at ? t("poolDueAt", { time: new Date(item.due_at).toLocaleString() }) : t("poolNoDeadline")}
                </span>{" "}
                {item.overdue_count > 0 && <span>{t("poolOverdue", { count: item.overdue_count })} </span>}
                {item.claimed ? (
                  t("poolClaimed")
                ) : item.deadline_passed ? (
                  t("poolExpired")
                ) : (
                  <Button disabled={busy || !item.can_claim} onClick={() => claim(item.package_id)}>
                    {t("poolClaim")}
                  </Button>
                )}
              </li>
            ))}
          </ul>
          {!data.results.length && <p>{t("poolEmpty")}</p>}
          <Button disabled={busy || !data.previous} onClick={() => load(page - 1)}>
            {t("previous")}
          </Button>{" "}
          <span>{t("page", { page })}</span>{" "}
          <Button disabled={busy || !data.next} onClick={() => load(page + 1)}>
            {t("next")}
          </Button>
        </>
      )}
      <Button disabled={busy} onClick={() => load(page)}>
        {t("poolReload")}
      </Button>
    </section>
  );
};
