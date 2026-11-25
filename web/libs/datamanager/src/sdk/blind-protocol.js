// A participant session must never silently fall back to a raw-ID route.
// This adapter is dormant until its caller explicitly enables blindProtocol.
const HANDLE_MIN = 2 ** 51;
const HANDLE_MAX = 2 ** 52;

export const blindEndpoints = Object.freeze({
  blindProject: { path: "/blind-project/", method: "get" },
  blindColumns: { path: "/blind-columns/", method: "get" },
  blindWork: { path: "/../projects/:projectID/blind-work/", method: "get" },
  blindTask: { path: "/../tasks/blind/:taskID/", method: "get" },
  blindSubmitAnnotation: { path: "/../tasks/blind/:taskID/annotations/", method: "post" },
  blindUpdateAnnotation: { path: "/../tasks/blind/:taskID/annotations/:annotationID/", method: "patch" },
  blindCreateDraftForTask: { path: "/../tasks/blind/:taskID/drafts", method: "post" },
  blindCreateDraftForAnnotation: { path: "/../tasks/blind/:taskID/annotations/:annotationID/drafts", method: "post" },
  blindUpdateDraft: { path: "/../tasks/blind/:taskID/drafts/:draftID/", method: "patch" },
  blindDeleteDraft: { path: "/../tasks/blind/:taskID/drafts/:draftID/", method: "delete" },
});
Object.values(blindEndpoints).forEach(Object.freeze);

const handles = (value) => {
  if (typeof value !== "number" && typeof value !== "string") return false;
  if (typeof value === "string" && !/^[0-9]{1,16}$/.test(value)) return false;
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= HANDLE_MIN && number < HANDLE_MAX;
};

export function validBlindWorkPage(value, page, project) {
  if (!value || !Array.isArray(value.results) || value.page !== page || typeof value.has_next !== "boolean"
      || !Number.isSafeInteger(value.total) || value.total < value.results.length || value.results.length > 50
      || !projectID(project)) return false;
  const tasks = new Set();
  const assignments = new Set();
  return value.results.every((row) => {
    if (!row || typeof row.task_id !== "number" || !handles(row.task_id) || row.id !== row.task_id
        || typeof row.assignment_id !== "number" || !handles(row.assignment_id)
        || !Number.isSafeInteger(row.project) || String(row.project) !== String(Number(project))
        || !Number.isSafeInteger(row.assignment_version) || row.assignment_version <= 0
        || typeof row.can_label !== "boolean" || !row.data || typeof row.data !== "object" || Array.isArray(row.data)
        || !Object.keys(row.data).length || !Object.values(row.data).every((text) => typeof text === "string")
        || tasks.has(row.task_id) || assignments.has(row.assignment_id)) return false;
    tasks.add(row.task_id);
    assignments.add(row.assignment_id);
    return true;
  });
}

const projectID = (value) => {
  if (typeof value !== "number" && typeof value !== "string") return false;
  if (typeof value === "string" && !/^[0-9]{1,16}$/.test(value)) return false;
  return Number.isSafeInteger(Number(value)) && Number(value) > 0;
};

export function validBlindTask(value, taskId, project) {
  if (!value || typeof value.id !== "number" || !handles(value.id) || String(value.id) !== String(taskId)
      || !Number.isSafeInteger(value.project) || !projectID(project) || value.project !== Number(project)
      || typeof value.assignment_id !== "number" || !handles(value.assignment_id)
      || !Number.isSafeInteger(value.assignment_version) || value.assignment_version < 1
      || typeof value.can_label !== "boolean" || !value.data || typeof value.data !== "object"
      || Array.isArray(value.data) || !Object.keys(value.data).length
      || !Object.values(value.data).every((text) => typeof text === "string")
      || !Array.isArray(value.annotations) || value.annotations.length > 1
      || !Array.isArray(value.drafts) || value.drafts.length > 1
      || !Array.isArray(value.predictions) || value.predictions.length) return false;
  const annotation = value.annotations[0];
  if (annotation && (typeof annotation.id !== "number" || !handles(annotation.id)
      || annotation.task !== value.id || !Array.isArray(annotation.result))) return false;
  if (value.default_selected_annotation !== (annotation?.id ?? null)) return false;
  const draft = value.drafts[0];
  if (draft && (typeof draft.id !== "number" || !handles(draft.id) || draft.task !== value.id
      || !Array.isArray(draft.result) || !Number.isSafeInteger(draft.revision) || draft.revision < 1
      || (draft.annotation !== null && draft.annotation !== annotation?.id))) return false;
  return true;
}

const bootstrap = { project: "blindProject", columns: "blindColumns" };

const routes = {
  task: ["blindTask", []],
  submitAnnotation: ["blindSubmitAnnotation", []],
  updateAnnotation: ["blindUpdateAnnotation", ["annotationID"]],
  createDraftForTask: ["blindCreateDraftForTask", []],
  createDraftForAnnotation: ["blindCreateDraftForAnnotation", ["annotationID"]],
  updateDraft: ["blindUpdateDraft", ["draftID"]],
  deleteDraft: ["blindDeleteDraft", ["draftID"]],
};

export function blindRequest(methodName, input, currentTaskId, currentProjectId) {
  if (Object.prototype.hasOwnProperty.call(bootstrap, methodName)) {
    if (!projectID(currentProjectId)) return null;
    return { methodName: bootstrap[methodName], params: { project: currentProjectId } };
  }
  if (methodName === "workList") {
    if (!projectID(currentProjectId)) return null;
    if (input != null && (typeof input !== "object" || Array.isArray(input)
        || Object.keys(input).some((key) => key !== "page"))) return null;
    const page = input?.page ?? 1;
    if (!projectID(page) || Number(page) > 1000) return null;
    return { methodName: "blindWork", params: { projectID: currentProjectId, page } };
  }
  if (!Object.prototype.hasOwnProperty.call(routes, methodName)) return null;
  const route = routes[methodName];
  const params = { ...(input ?? {}) };
  const taskID = params.taskID ?? currentTaskId;
  if (!handles(taskID) || route[1].some((key) => !handles(params[key]))) return null;
  // Read can navigate to a different handle; a write stays on the loaded task.
  if (methodName !== "task" && (!handles(currentTaskId) || String(taskID) !== String(currentTaskId))) return null;
  const scoped = { taskID };
  for (const key of route[1]) scoped[key] = params[key];
  return { methodName: route[0], params: scoped };
}

export function blindEndpointAllowed(methodName, params, sharedParams, endpoint, parentPath) {
  if (Object.keys(sharedParams ?? {}).length) return false;
  const isBootstrap = Object.values(bootstrap).includes(methodName);
  const isWorkList = methodName === "blindWork";
  const route = Object.values(routes).find(([name]) => name === methodName);
  if ((!route && !isBootstrap && !isWorkList) || !params || typeof params !== "object") return false;
  const expected = blindEndpoints[methodName];
  if (!endpoint || endpoint.path !== expected.path || String(endpoint.method).toLowerCase() !== expected.method
      || endpoint.gateway !== undefined || (parentPath ?? []).some(Boolean)) return false;
  if (isBootstrap) return Object.keys(params).length === 1 && projectID(params.project);
  if (isWorkList) return Object.keys(params).length === 2 && projectID(params.projectID)
    && projectID(params.page) && Number(params.page) <= 1000;
  const allowed = ["taskID", ...route[1]];
  return Object.keys(params).every((key) => allowed.includes(key))
    && allowed.every((key) => handles(params[key]));
}
