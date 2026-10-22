export function selectedTaskPackageIds(store) {
  // Selections belong to the current view's ID domain; annotation IDs are not task IDs.
  if (store?.target !== "tasks") return [];
  const selection = store.currentSelection;
  // Never expand select-all, exclusions or live filters into a fixed task list.
  return selection?.all === false ? [...(selection.included ?? [])] : [];
}
