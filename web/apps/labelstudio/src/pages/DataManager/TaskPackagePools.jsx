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
  const [assignee, setAssignee] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const dispatchRequest = useRef(null);
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
          due_at: deadlineDirty ? (deadline ? new Date(deadline).toISOString() : null) : pool.due_at,
        },
        errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (failed(response)) {
        if (denied(response)) setPool(null);
        setError("poolSaveFailed");
      } else {
        setPool({ ...response, claimed: pool.claimed });
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
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = async (number = 1) => {
    const current = ++generation.current;
    setBusy(true);
    setError("");
    setData(null);
    try {
      const response = await api.current("taskPackagePools", {
        params: { pk: projectId, page: number, page_size: 50 },
        errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (failed(response)) setError("poolLoadFailed");
      else {
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
        if (denied(response)) setData(null);
        setError("claimFailed");
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
      {data && (
        <>
          <ul>
            {data.results.map((item) => (
              <li key={item.package_id}>
                <span>{item.name}</span>{" "}
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
