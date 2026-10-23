// Display-only projections of the authorized detail response, never new workflow state.
export const batchProgress = (items) => {
  const selected = items.filter((item) => item.selected);
  const count = (status) => selected.filter((item) => item.status === status).length;
  return {
    selected: selected.length,
    unchecked: items.length - selected.length,
    pending: count("pending"),
    approved: count("approved"),
    rejected: count("rejected"),
    superseded: count("superseded"),
    unavailable: count("unavailable"),
  };
};

export const filterBatchItems = (items, filter) => items.filter((item) => {
  switch (filter) {
    case "selectedPending": return item.selected && item.status === "pending";
    case "selectedReviewed": return item.selected && ["approved", "rejected"].includes(item.status);
    case "reworkRelated": return item.status === "rejected";
    case "unselected": return !item.selected;
    default: return true;
  }
});
