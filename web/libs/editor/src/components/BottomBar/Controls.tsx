/**
 * This panel is used with FF_1170 + FF_3873 in new interface,
 * but it's also used in old interface with FF_3873, but without FF_1170.
 * Only this component should get interface updates, other versions should be removed.
 */

import { observer } from "mobx-react";
import type React from "react";
import { useCallback, useState } from "react";

import { Button, ButtonGroup, type ButtonProps } from "@humansignal/ui";
import { IconBan, IconChevronDown } from "@humansignal/icons";
import { Dropdown } from "@humansignal/ui";
import type { CustomButtonType } from "../../stores/CustomButton";
import { cn } from "../../utils/bem";
import { useLocaleTranslation } from "@humansignal/i18n";
import { FF_REVIEWER_FLOW, FF_FIT_1304_STRICT_OVERLAP, isFF } from "../../utils/feature-flags";
import { isDefined, toArray } from "../../utils/utilities";
import {
  AcceptButton,
  ButtonTooltip,
  controlsInjector,
  RejectButtonDefinition,
  SkipButton,
  UnskipButton,
} from "./buttons";

import "./Controls.scss";

// these buttons can be reused inside custom buttons or can be replaces with custom buttons
type SupportedInternalButtons = "accept" | "reject";
// special places for custom buttons — before, after or instead of internal buttons
type SpecialPlaces = "_before" | "_after" | "_replace";
// @todo should be Instance<typeof AppStore>["customButtons"] but it doesn't fit to itself
type CustomButtonsField = Map<
  SpecialPlaces | SupportedInternalButtons,
  CustomButtonType | SupportedInternalButtons | Array<CustomButtonType | SupportedInternalButtons>
>;
type ControlButtonProps = {
  button: CustomButtonType;
  disabled: boolean;
  variant?: ButtonProps["variant"];
  look?: ButtonProps["look"];
  onClick: (e: React.MouseEvent) => void;
  builtinReject?: boolean;
};

export const EMPTY_SUBMIT_TOOLTIP = "Empty annotations denied in this project";
export const INCOMPLETE_SUBMIT_TOOLTIP = "Complete all regions before submitting";
export const INCOMPLETE_UPDATE_TOOLTIP = "Complete all regions before updating";
export const INCOMPLETE_ACCEPT_TOOLTIP = "Complete all regions before accepting";

/**
 * Custom action button component, rendering buttons from store.customButtons
 */
const ControlButton = observer(({ button, disabled, onClick, variant, look, builtinReject }: ControlButtonProps) => {
  const { t } = useLocaleTranslation("editor");
  return (
    <Button
      {...button.props}
      variant={button.variant ?? variant}
      look={button.look ?? look}
      tooltip={builtinReject ? t("rejectTooltip") : button.tooltip}
      className="w-[150px]"
      aria-label={builtinReject ? t("reject") : button.ariaLabel}
      disabled={button.disabled || disabled}
      onClick={onClick}
      data-testid={`bottombar-custom-${button.name}-button`}
    >
      {builtinReject ? t("reject") : button.title}
    </Button>
  );
});

export const Controls = controlsInjector<{ annotation: MSTAnnotation }>(
  observer(({ store, history, annotation }) => {
    const { t } = useLocaleTranslation("editor");
    const isReview = store.hasInterface("review") || annotation.canBeReviewed;
    const isNotQuickView = store.hasInterface("topbar:prevnext");
    const historySelected = isDefined(store.annotationStore.selectedHistory);
    const { userGenerate, sentUserGenerate, versions, results, editable: annotationEditable } = annotation;
    const dropdownTrigger = cn("dropdown").elem("trigger").toClassName();
    const customButtons: CustomButtonsField = store.customButtons;
    const buttons: React.ReactNode[] = [];

    const [isInProgress, setIsInProgress] = useState(false);
    const disabled = !annotationEditable || store.isSubmitting || historySelected || isInProgress;
    // Reviewing an immutable annotation is separate from editing its results.
    const writeDisabled = disabled || annotation.isReadOnly?.() === true || !!annotation.draftConflict;
    const submitDisabled = store.hasInterface("annotations:deny-empty") && results.length === 0;
    const hasIncompleteRegions = annotation.hasIncompletePolygons;

    /** Check all things related to comments and then call the action if all is good */
    const handleActionWithComments = useCallback(
      async (e: React.MouseEvent, callback: () => any, errorMessage: string) => {
        const { addedCommentThisSession, currentComment, commentFormSubmit } = store.commentStore;
        const comment = currentComment[annotation.id];
        // accept both old and new comment formats
        const commentText = (comment?.text ?? comment)?.trim();

        if (isInProgress) return;
        setIsInProgress(true);

        const selected = store.annotationStore?.selected;

        if (addedCommentThisSession) {
          selected?.submissionInProgress();
          callback();
        } else if (commentText) {
          e.preventDefault();
          selected?.submissionInProgress();
          await commentFormSubmit();
          callback();
        } else {
          store.commentStore.setTooltipMessage(errorMessage);
        }
        setIsInProgress(false);
      },
      [
        store.rejectAnnotation,
        store.skipTask,
        store.commentStore.currentComment,
        store.commentStore.commentFormSubmit,
        store.commentStore.addedCommentThisSession,
        isInProgress,
      ],
    );

    if (annotation.isNonEditableDraft) return <></>;

    const buttonsBefore = customButtons.get("_before");
    const buttonsReplacement = customButtons.get("_replace");
    const firstToRender = buttonsReplacement ?? buttonsBefore;

    // either we render _before buttons and then the rest, or we render only _replace buttons
    if (firstToRender) {
      const allButtons = toArray(firstToRender);
      for (const customButton of allButtons) {
        // @todo make a list of all internal buttons and use them here to mix custom buttons with internal ones
        // string buttons is a way to render internal buttons
        if (typeof customButton === "string") {
          if (customButton === "accept") {
            // just an example of internal button usage
            // @todo move buttons to separate components
            buttons.push(<AcceptButton key={customButton} disabled={disabled} history={history} store={store} />);
          }
        } else {
          buttons.push(
            <ControlButton
              key={customButton.name}
              disabled={disabled}
              button={customButton}
              onClick={() => store.handleCustomButton?.(customButton)}
            />,
          );
        }
      }
    }

    if (buttonsReplacement) {
      return <div className={cn("controls").toClassName()}>{buttons}</div>;
    }

    if (isReview) {
      const customRejectButtons = toArray(customButtons.get("reject"));
      const hasCustomReject = customRejectButtons.length > 0;
      const originalRejectButton = RejectButtonDefinition;

      // @todo implement reuse of internal buttons later (they are set as strings)
      const rejectButtons: CustomButtonType[] = hasCustomReject
        ? customRejectButtons.filter((button) => typeof button !== "string")
        : [originalRejectButton];

      rejectButtons.forEach((button) => {
        const action = hasCustomReject ? () => store.handleCustomButton?.(button) : () => store.rejectAnnotation({});

        const onReject = async (e: React.MouseEvent) => {
          const selected = store.annotationStore?.selected;

          if (store.hasInterface("comments:reject")) {
            handleActionWithComments(e, action, t("commentBeforeReject"));
          } else {
            selected?.submissionInProgress();
            await store.commentStore.commentFormSubmit();
            action();
          }
        };

        buttons.push(<ControlButton key={button.name} button={button} builtinReject={!hasCustomReject} disabled={disabled} onClick={onReject} />);
      });
      buttons.push(<AcceptButton key="review-accept" disabled={disabled} history={history} store={store} />);
    } else if (annotation.skipped) {
      buttons.push(
        <div className={cn("controls").elem("skipped-info").toClassName()} key="skipped">
          <IconBan /> {t("wasSkipped")}
        </div>,
      );
      buttons.push(<UnskipButton key="unskip" disabled={writeDisabled} store={store} />);
    } else {
      if (store.hasInterface("skip")) {
        const onSkipWithComment = (e: React.MouseEvent, action: () => any) => {
          handleActionWithComments(e, action, t("commentBeforeSkip"));
        };

        buttons.push(<SkipButton key="skip" disabled={writeDisabled} store={store} onSkipWithComment={onSkipWithComment} />);
      }

      // Also disable when overlap is reached (only when feature flag is enabled)
      const overlapDisabled = isFF(FF_FIT_1304_STRICT_OVERLAP) && store.overlapReached === true;
      const isDisabled = writeDisabled || submitDisabled || overlapDisabled || hasIncompleteRegions;

      const useExitOption = !isDisabled && isNotQuickView;

      const SubmitOption = ({ isUpdate, onClickMethod }: { isUpdate: boolean; onClickMethod: () => any }) => {
        return (
          <div className="p-tighter rounded">
            <Button
              name="submit-option"
              look="string"
              size="small"
              className="w-[150px]"
              onClick={async (event) => {
                event.preventDefault();

                if ("URLSearchParams" in window) {
                  const searchParams = new URLSearchParams(window.location.search);

                  searchParams.set("exitStream", "true");
                  const newRelativePathQuery = `${window.location.pathname}?${searchParams.toString()}`;

                  window.history.pushState(null, "", newRelativePathQuery);
                }

                await store.commentStore.commentFormSubmit();
                onClickMethod();
              }}
              data-testid={`bottombar-${isUpdate ? "update" : "submit"}-and-exit-button`}
            >
              {isUpdate ? t("updateAndExit") : t("submitAndExit")}
            </Button>
          </div>
        );
      };

      if (userGenerate || (store.explore && !userGenerate && store.hasInterface("submit"))) {
        const title = hasIncompleteRegions
          ? t("incompleteSubmit")
          : overlapDisabled
            ? store.overlapReachedMessage
            : submitDisabled
              ? t("emptySubmitDenied")
              : t("submitTooltip");

        buttons.push(
          <ButtonTooltip key="submit" title={title} className="whitespace-nowrap max-w-none">
            <div className={cn("controls").elem("tooltip-wrapper").toClassName()}>
              <ButtonGroup>
                <Button
                  aria-label={t("submitCurrentAnnotation")}
                  name="submit"
                  className="w-[150px]"
                  disabled={isDisabled}
                  onClick={async (event) => {
                    if ((event.target as HTMLButtonElement).classList.contains(dropdownTrigger)) return;
                    await store.commentStore.commentFormSubmit();
                    store.submitAnnotation();
                  }}
                  data-testid="bottombar-submit-button"
                >
                  {t("submit")}
                </Button>
                {useExitOption ? (
                  <Dropdown.Trigger
                    alignment="top-right"
                    content={
                      <div className="p-tight bg-neutral-surface">
                        <SubmitOption onClickMethod={store.submitAnnotation} isUpdate={false} />
                      </div>
                    }
                  >
                    <Button
                      disabled={isDisabled}
                      aria-label={t("submitAnnotation")}
                      data-testid="bottombar-submit-dropdown"
                    >
                      <IconChevronDown />
                    </Button>
                  </Dropdown.Trigger>
                ) : null}
              </ButtonGroup>
            </div>
          </ButtonTooltip>,
        );
      } else if ((userGenerate && sentUserGenerate) || (!userGenerate && store.hasInterface("update"))) {
        const isUpdate = Boolean(isFF(FF_REVIEWER_FLOW) || sentUserGenerate || versions.result);
        // no changes were made over previously submitted version — no drafts, no pending changes
        const noChanges = isFF(FF_REVIEWER_FLOW) && !history.canUndo && !annotation.draftId;
        const isUpdateDisabled = isDisabled || noChanges;
        const updateTitle = hasIncompleteRegions
          ? t("incompleteUpdate")
          : overlapDisabled
            ? store.overlapReachedMessage
            : noChanges
              ? t("noChanges")
              : t("updateTooltip");
        const button = (
          <ButtonTooltip key="update" title={updateTitle} className="whitespace-nowrap max-w-none">
            <div className={cn("controls").elem("tooltip-wrapper").toClassName()}>
              <ButtonGroup>
                <Button
                  aria-label={isUpdate ? t("updateAnnotation") : t("submitAnnotation")}
                  name="submit"
                  className="w-[150px]"
                  disabled={isUpdateDisabled}
                  onClick={async (event) => {
                    if ((event.target as HTMLButtonElement).classList.contains(dropdownTrigger)) return;
                    await store.commentStore.commentFormSubmit();
                    store.updateAnnotation();
                  }}
                  data-testid="bottombar-update-button"
                >
                  {isUpdate ? t("update") : t("submit")}
                </Button>
                {useExitOption ? (
                  <Dropdown.Trigger
                    alignment="top-right"
                    content={<SubmitOption onClickMethod={store.updateAnnotation} isUpdate={isUpdate} />}
                  >
                    <Button
                      disabled={isUpdateDisabled}
                      aria-label={isUpdate ? t("updateAnnotation") : t("submitAnnotation")}
                      data-testid="bottombar-update-dropdown"
                    >
                      <IconChevronDown />
                    </Button>
                  </Dropdown.Trigger>
                ) : null}
              </ButtonGroup>
            </div>
          </ButtonTooltip>
        );

        buttons.push(button);
      }
    }

    return <div className={cn("controls").toClassName()}>{buttons}</div>;
  }),
);
