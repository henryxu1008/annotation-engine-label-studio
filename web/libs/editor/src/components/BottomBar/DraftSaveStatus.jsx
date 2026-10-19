import { observer } from "mobx-react";
import { useState } from "react";
import { createPortal } from "react-dom";
import { Button, Tooltip } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { cn } from "../../utils/bem";

const DraftConflict = observer(({ annotation }) => {
  const { t } = useLocaleTranslation("editor");
  const [backedUp, setBackedUp] = useState(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(null);
  const backup = annotation.draftBackup;
  const missing = annotation.draftConflictReason === "missing";
  return createPortal(<details open className={cn("bottombar").elem("draft-conflict").toClassName()} data-testid="draft-conflict">
    <summary>{t(missing ? "draftMissing" : "draftConflict")}</summary>
    <p role="alert">{t(missing ? "draftMissingHint" : "draftConflictHint")}</p>
    {annotation.hasPendingTextInput && <p role="alert">{t("confirmTextInput")}</p>}
    <textarea aria-label={t("draftLocalCopy")} value={backup} readOnly rows={5} />
    <Button size="small" data-testid="draft-backup" disabled={annotation.hasPendingTextInput} onClick={() => {
      const currentCopy = annotation.draftBackup;
      const url = URL.createObjectURL(new Blob([currentCopy], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `task-${annotation.store.task.id}-unsaved-draft.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setBackedUp(currentCopy);
    }}>{t("draftDownloadCopy")}</Button>
    <Button size="small" data-testid="draft-reload-latest" disabled={loading || backedUp !== backup || annotation.hasPendingTextInput} onClick={async () => {
      const requestedCopy = annotation.draftBackup;
      if (backedUp !== requestedCopy) return;
      setLoading(true);
      setFailed(null);
      try {
        if (!(await annotation.reloadLatestDraft())) setFailed(
          annotation.hasPendingTextInput || annotation.draftBackup !== requestedCopy ? "changed" : "load",
        );
      }
      catch { setFailed("load"); }
      finally { setLoading(false); }
    }}>{t(missing ? "draftReloadTask" : "draftReloadLatest")}</Button>
    {failed && <p role="alert">{t(failed === "changed" ? "draftReloadChanged" : missing ? "draftReloadTaskFailed" : "draftReloadFailed")}</p>}
  </details>, document.body);
});

export const DraftSaveStatus = observer(({ annotation }) => {
  const { t } = useLocaleTranslation("editor");
  if (!annotation?.hasUnsavedDraft && !annotation?.hasPendingTextInput) return null;
  const saving = annotation.isDraftSaving || !!annotation.submissionStarted;
  if (annotation.draftConflict) return <DraftConflict key={annotation.id} annotation={annotation} />;
  if (annotation.hasPendingTextInput) return (
    <span role="status" aria-live="polite" data-testid="text-input-pending">{t("confirmTextInput")}</span>
  );
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
