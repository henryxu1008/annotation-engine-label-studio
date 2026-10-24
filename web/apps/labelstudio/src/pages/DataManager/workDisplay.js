export const displayWorkState = (state, t) => {
  switch (state) {
    case "assigned":
      return t("myWorkAssigned");
    case "in_progress":
      return t("myWorkInProgress");
    case "rework":
      return t("myWorkRework");
    case "submitted":
      return t("myWorkSubmitted");
    case "approved":
      return t("myWorkApproved");
    case "conflict":
      return t("myWorkConflict");
    case "pending":
      return t("myWorkPendingReview");
    default:
      return state;
  }
};
