import { observer } from "mobx-react";
import { Button, Tooltip } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";

export const DraftSaveStatus = observer(({ annotation }) => {
  const { t } = useLocaleTranslation("editor");
  if (!annotation?.hasUnsavedDraft) return null;
  const saving = annotation.isDraftSaving || !!annotation.submissionStarted;
  return (
    <div className="bottombar__draft-status" data-testid="draft-save-status">
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
