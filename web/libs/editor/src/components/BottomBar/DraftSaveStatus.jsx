import { observer } from "mobx-react";
import { useState } from "react";
import { Button, Tooltip } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { cn } from "../../utils/bem";

const DraftConflict = observer(({ annotation }) => {
  const { t } = useLocaleTranslation("editor");
  const [backedUp, setBackedUp] = useState(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const backup = JSON.stringify({
    task_id: annotation.store.task.id,
    draft_id: annotation.draftId,
    draft_revision: annotation.draftRevision,
    result: annotation.serializeAnnotation(),
  }, null, 2);
  return <details open className={cn("bottombar").elem("draft-conflict").toClassName()} data-testid="draft-conflict">
    <summary>{t("draftConflict")}</summary>
    <p role="alert">{t("draftConflictHint")}</p>
    <textarea aria-label={t("draftLocalCopy")} value={backup} readOnly rows={5} />
    <Button size="small" data-testid="draft-backup" onClick={() => {
      const url = URL.createObjectURL(new Blob([backup], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `task-${annotation.store.task.id}-unsaved-draft.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setBackedUp(backup);
    }}>{t("draftDownloadCopy")}</Button>
    <Button size="small" data-testid="draft-reload-latest" disabled={loading || backedUp !== backup} onClick={async () => {
      setLoading(true);
      setFailed(false);
      try { if (!(await annotation.reloadLatestDraft())) setFailed(true); }
      catch { setFailed(true); }
      finally { setLoading(false); }
    }}>{t("draftReloadLatest")}</Button>
    {failed && <p role="alert">{t("draftReloadFailed")}</p>}
  </details>;
});

export const DraftSaveStatus = observer(({ annotation }) => {
  const { t } = useLocaleTranslation("editor");
  if (!annotation?.hasUnsavedDraft) return null;
  const saving = annotation.isDraftSaving || !!annotation.submissionStarted;
  if (annotation.draftConflict) return <DraftConflict key={annotation.id} annotation={annotation} />;
  return (
    <div className={cn("bottombar").elem("draft-status").toClassName()} data-testid="draft-save-status">
      <Tooltip title={t("draftLeaveHint")}>
        <span role="status" aria-live="polite">
          {t(saving ? "draftSaving" : annotation.draftSaveError ? "draftSaveFailed" : "draftUnsaved")}
        </span>
      </Tooltip>
      <Button
        size="small"
        disabled={saving}
        data-testid="draft-save-retry"
        onClick={() => { annotation.saveDraftImmediatelyWithResults().catch(() => {}); }}
      >
        {t(annotation.draftSaveError ? "draftRetry" : "draftSave")}
      </Button>
    </div>
  );
});
