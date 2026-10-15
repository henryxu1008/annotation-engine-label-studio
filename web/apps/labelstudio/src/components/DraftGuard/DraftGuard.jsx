import { useContext, useEffect } from "react";
import { useHistory } from "react-router-dom";
import { ToastContext } from "@humansignal/ui";
import { useLocaleRuntime } from "@humansignal/i18n";
import { isAlive, isStateTreeNode } from "mobx-state-tree";

export const DRAFT_GUARD_KEY = "DRAFT_GUARD";

export const draftGuardCallback = {
  current: null,
};

export const DraftGuard = () => {
  const { show: showToast } = useContext(ToastContext);
  const history = useHistory();
  const runtime = useLocaleRuntime();

  useEffect(() => {
    // Hard navigation cannot wait for an async draft flush. The browser owns
    // this dialog's wording; cancel leaves the editable page available to retry.
    const beforeUnload = (event) => {
      const editor = window.Htx;
      if (isStateTreeNode(editor) && !isAlive(editor)) return;
      const selected = editor?.annotationStore?.selected;
      if (isStateTreeNode(selected) && !isAlive(selected)) return;
      if (!selected?.hasUnsavedDraft) return;
      event.preventDefault();
      event.returnValue = true;
    };
    window.addEventListener("beforeunload", beforeUnload);
    const unblock = () => {
      draftGuardCallback.current?.(true);
      draftGuardCallback.current = null;
    };

    /**
     * The version of Router History that is in use does not currently support
     * the `block` method fully. This is a workaround to allow us to block navigation
     * when there are unsaved changes. The draftGuardCallback allows the unblock callback to be captured from the
     * history callback `getUserConfirmation` that is triggered by returning a string message from history.block, allowing the user to
     * confirm they want to leave the page. Here we send through a constant message
     * to signify that we aren't looking for user confirmation but to utilize this to enable navigation blocking based on
     * unsuccessful draft saves.
     */
    const unsubscribe = history.block(() => {
      const editor = window.Htx;
      if (isStateTreeNode(editor) && !isAlive(editor)) return;
      const selected = editor?.annotationStore?.selected;
      if (isStateTreeNode(selected) && !isAlive(selected)) return;
      const submissionInProgress = !!selected?.submissionStarted;
      const hasChanges = !!selected?.hasUnsavedDraft && !submissionInProgress;

      if (hasChanges) {
        const showSaveError = () => showToast({ message: runtime.t("datamanager:draftSaveError"), type: "error" });
        selected.saveDraftImmediatelyWithResults()?.then((res) => {
          const status = res?.$meta?.status;

          if (status === 200 || status === 201) {
            showToast({ message: runtime.t("datamanager:draftSaved"), type: "info" });
            unblock();
          } else {
            showSaveError();
          }
        }).catch(showSaveError);

        return DRAFT_GUARD_KEY;
      }
    });

    return () => {
      unblock();
      unsubscribe();
      window.removeEventListener("beforeunload", beforeUnload);
    };
  }, [history, runtime, showToast]);

  return <></>;
};
