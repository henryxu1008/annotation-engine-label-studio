import { blindRequest, validBlindTask, validBlindWorkPage } from "./blind-protocol";
import { APIConfig } from "./api-config";

const task = 2 ** 51 + 11;
const annotation = 2 ** 51 + 12;
const draft = 2 ** 51 + 13;

it("routes reads and all supported writes through participant handles", () => {
  expect(blindRequest("task", { taskID: String(task) })).toEqual({
    methodName: "blindTask", params: { taskID: String(task) },
  });
  expect(blindRequest("task", { taskID: task, project: 7, selected: [1], task_package: 3 })).toEqual({
    methodName: "blindTask", params: { taskID: task },
  });
  expect(blindRequest("updateAnnotation", { taskID: task, annotationID: annotation }, task)).toEqual({
    methodName: "blindUpdateAnnotation", params: { taskID: task, annotationID: annotation },
  });
  expect(blindRequest("updateDraft", { draftID: draft }, task)).toEqual({
    methodName: "blindUpdateDraft", params: { taskID: task, draftID: draft },
  });
  expect(blindRequest("deleteDraft", { draftID: draft }, task)?.methodName).toBe("blindDeleteDraft");
  expect(blindRequest("createDraftForAnnotation", { taskID: task, annotationID: annotation }, task)?.methodName)
    .toBe("blindCreateDraftForAnnotation");
  expect(blindRequest("createDraftForTask", { taskID: task }, task)?.methodName).toBe("blindCreateDraftForTask");
  expect(blindRequest("submitAnnotation", { taskID: task }, task)?.methodName).toBe("blindSubmitAnnotation");
});

it("fails closed for old endpoints, raw IDs, absent state and a different active task", () => {
  for (const name of ["tasks", "nextTask", "fetchAnnotation", "deleteAnnotation", "convertToDraft", "invokeAction", "toString"]) {
    expect(blindRequest(name, { taskID: task }, task)).toBeNull();
  }
  expect(blindRequest("task", { taskID: 123 })).toBeNull();
  expect(blindRequest("task", { taskID: [task] })).toBeNull();
  expect(blindRequest("task", { taskID: `${task}.0` })).toBeNull();
  expect(blindRequest("updateDraft", { draftID: 7 }, task)).toBeNull();
  expect(blindRequest("updateDraft", { draftID: draft })).toBeNull();
  expect(blindRequest("submitAnnotation", { taskID: task + 1 }, task)).toBeNull();
  expect(blindRequest("updateAnnotation", { taskID: task, annotationID: annotation }, task + 1)).toBeNull();
});

it("uses only the bound project for participant bootstrap", () => {
  expect(blindRequest("project", { project: 99, taskID: 42 }, undefined, 7)).toEqual({
    methodName: "blindProject", params: { project: 7 },
  });
  expect(blindRequest("columns", {}, undefined, "7")).toEqual({
    methodName: "blindColumns", params: { project: "7" },
  });
  expect(blindRequest("workList", { page: 2 }, undefined, 7)).toEqual({
    methodName: "blindWork", params: { projectID: 7, page: 2 },
  });
  expect(blindRequest("workList", { page: 2, filter: "anything" }, undefined, 7)).toBeNull();
  expect(blindRequest("workList", { page: 1001 }, undefined, 7)).toBeNull();
  expect(blindRequest("project", {}, undefined, undefined)).toBeNull();
  expect(blindRequest("project", {}, undefined, "7.0")).toBeNull();
});

it("rejects stale or malformed personal work before retaining it", () => {
  const row = { id: task, task_id: task, project: 7, assignment_id: annotation, assignment_version: 1,
    can_label: true, data: { text: "example" } };
  const value = { results: [row], page: 1, total: 1, has_next: false };
  expect(validBlindWorkPage(value, 1, "7")).toBe(true);
  expect(validBlindWorkPage(value, 1, 8)).toBe(false);
  expect(validBlindWorkPage({ ...value, results: [row, row], total: 2 }, 1, 7)).toBe(false);
  expect(validBlindWorkPage({ ...value, results: [{ ...row, id: 1 }] }, 1, 7)).toBe(false);
  expect(validBlindWorkPage({ ...value, results: [{ ...row, data: { text: 3 } }] }, 1, 7)).toBe(false);
  expect(validBlindWorkPage({ ...value, total: 0 }, 1, 7)).toBe(false);
});

it("binds editor data and nested resources to the requested task and project", () => {
  const value = { id: task, project: 7, assignment_id: annotation, assignment_version: 1, can_label: true,
    data: { text: "example" }, annotations: [{ id: draft, task, result: [] }], drafts: [], predictions: [],
    default_selected_annotation: draft };
  expect(validBlindTask(value, String(task), "7")).toBe(true);
  expect(validBlindTask(value, task + 1, 7)).toBe(false);
  expect(validBlindTask(value, task, 8)).toBe(false);
  expect(validBlindTask({ ...value, assignment_id: 3 }, task, 7)).toBe(false);
  expect(validBlindTask({ ...value, annotations: [{ ...value.annotations[0], id: 1 }] }, task, 7)).toBe(false);
  expect(validBlindTask({ ...value, annotations: [{ ...value.annotations[0], task: task + 1 }] }, task, 7)).toBe(false);
  expect(validBlindTask({ ...value, predictions: [{ id: 1 }] }, task, 7)).toBe(false);
  expect(validBlindTask({ ...value, default_selected_annotation: null }, task, 7)).toBe(false);
  expect(validBlindTask({ ...value, drafts: [{ id: annotation, task, annotation: 1, revision: 1, result: [] }] }, task, 7)).toBe(false);
});

it("binds each adapted operation to a handle-addressed API route", () => {
  for (const name of ["blindTask", "blindSubmitAnnotation", "blindUpdateAnnotation", "blindCreateDraftForTask",
    "blindCreateDraftForAnnotation", "blindUpdateDraft", "blindDeleteDraft"]) {
    const definition = APIConfig.endpoints[name];
    const path = typeof definition === "string" ? definition : definition.path;
    expect(path).toContain("/tasks/blind/:taskID/");
  }
});
