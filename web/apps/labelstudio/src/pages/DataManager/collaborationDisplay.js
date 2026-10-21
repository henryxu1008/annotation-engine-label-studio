import { formatDisplayDate } from "@humansignal/i18n";

// Translate only known display codes. API values and unknown extensions stay intact.
export const displayAssignmentStatus = (status, t) => {
  switch (status) {
    case "assigned": return t("assignmentStatusAssigned");
    case "in_progress": return t("assignmentStatusInProgress");
    case "cancelled": return t("assignmentStatusCancelled");
    default: return status;
  }
};

export const displaySubmissionStatus = (status, t) => {
  switch (status) {
    case "pending": return t("submissionStatusPending");
    case "approved": return t("submissionStatusApproved");
    case "rejected": return t("submissionStatusRejected");
    case "superseded": return t("submissionStatusSuperseded");
    default: return status;
  }
};

export const displayReviewDecision = (decision, t) => {
  switch (decision) {
    case "approved": return t("submissionStatusApproved");
    case "rejected": return t("submissionStatusRejected");
    default: return decision;
  }
};

export const collaborationErrorCode = (result, fallback) => {
  const status = result?.status ?? result?.$meta?.status;
  if (status === 403) return "accessDenied";
  if (status === 404) return "resourceUnavailable";
  return fallback;
};

export const displayCollaborationError = (code, t) => {
  switch (code) {
    case "accessDenied": return t("accessDenied");
    case "resourceUnavailable": return t("resourceUnavailable");
    case "memberChangeFailed": return t("memberChangeFailed");
    case "selectOrganizationUserFirst": return t("selectOrganizationUserFirst");
    case "assignmentsLoadFailed": return t("assignmentsLoadFailed");
    case "selectEligibleAssignee": return t("selectEligibleAssignee");
    case "assignFailed": return t("assignFailed");
    case "cancelAssignmentFailed": return t("cancelAssignmentFailed");
    case "reviewLoadFailed": return t("reviewLoadFailed");
    case "myWorkLoadFailed": return t("myWorkLoadFailed");
    case "rejectionReasonRequired": return t("rejectionReasonRequired");
    case "reviewSaveFailed": return t("reviewSaveFailed");
    case "submissionHistoryLoadFailed": return t("submissionHistoryLoadFailed");
    case "releaseFailed": return t("releaseFailed");
    case "releaseMismatch": return t("releaseMismatch");
    default: return t("unexpectedError");
  }
};

export const displayCollaborationDate = (value, locale) => {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return formatDisplayDate(date, locale, { dateStyle: "medium", timeStyle: "short" });
};
