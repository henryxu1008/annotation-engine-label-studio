// Resolve the session-bound protocol before allowing the data manager to load.
export async function loadWorkspaceBootstrap({ callApi, projectId, sessionUserId, isCancelled }) {
  if (isCancelled()) return null;
  const project = Number(projectId);
  const sessionUser = Number(sessionUserId);
  if (!Number.isSafeInteger(project) || project <= 0 || !Number.isSafeInteger(sessionUser) || sessionUser <= 0) {
    throw new Error("Work mode is unavailable");
  }
  const mode = await callApi("projectWorkMode", { params: { pk: project }, errorFilter: () => true });
  if (isCancelled()) return null;
  if (!mode || mode.error || mode.$meta?.ok === false || mode.project_id !== project
      || mode.session_user_id !== sessionUser || typeof mode.restricted !== "boolean") {
    throw new Error("Work mode is unavailable");
  }
  if (mode.restricted) return { restricted: true, interactiveBackend: null };
  const backends = await callApi("mlBackends", { params: { project } });
  if (isCancelled()) return null;
  return { restricted: false,
    interactiveBackend: Array.isArray(backends) ? backends.find(({ is_interactive }) => is_interactive) : null };
}
