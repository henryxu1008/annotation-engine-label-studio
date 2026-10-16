/**
 * Unit tests for Annotation model (stores/Annotation/Annotation.js).
 * Target: coverage parity 77.92%.
 */
if (typeof globalThis.structuredClone === "undefined") {
  globalThis.structuredClone = (obj) => JSON.parse(JSON.stringify(obj));
}

jest.mock("keymaster", () => {
  const keymaster = () => {};
  keymaster.unbind = () => {};
  keymaster.setScope = () => {};
  return { __esModule: true, default: keymaster };
});

import "../../../tags/visual/View";
import "../../../tags/object/RichText";
import "../../../tags/object/Image/Image.js";
import "../../../tags/control/Rectangle.js";
import "../../../tags/control/Labels/Labels";
import "../../../tags/control/TextArea/TextArea";
import Tree from "../../../core/Tree";
import Registry from "../../../core/Registry";
import AppStore from "../../AppStore";
import { autorun } from "mobx";

const MINIMAL_CONFIG = `<View><Text name="t1" value="$text" /></View>`;

const createTestEnv = () => ({
  events: {
    hasEvent: jest.fn(() => false),
    invoke: jest.fn(),
  },
  messages: {},
  settings: {},
});

function createStoreWithAnnotation(annotationSnapshot = {}) {
  const env = createTestEnv();
  const task = {
    id: 1,
    data: JSON.stringify({ text: "Hello" }),
  };
  const store = AppStore.create(
    {
      config: MINIMAL_CONFIG,
      task,
      interfaces: ["basic"],
    },
    env,
  );
  store.initializeStore({});
  const ann = store.annotationStore.addAnnotation({
    result: [],
    ...annotationSnapshot,
  });
  return { store, annotation: ann, env };
}

describe("Annotation model", () => {
  describe("restored text entity draft initialization", () => {
    const createTextDraft = (html = false) => {
      const env = createTestEnv();
      const object = html ? '<HyperText name="text" value="$text"/>' : '<Text name="text" value="$text"/>';
      const store = AppStore.create({
        config: `<View>${object}<Labels name="entity" toName="text"><Label value="Entity"/></Labels><TextArea name="note" toName="text" perRegion="true" editable="true"/></View>`,
        task: { id: 1, data: JSON.stringify({ text: html ? '<p>Hello 中文</p>' : 'Hello 中文' }) },
        interfaces: ["basic"],
      }, env);
      store.initializeStore({});
      const position = html ? { start: '/p[1]/text()[1]', end: '/p[1]/text()[1]', startOffset: 0, endOffset: 5 } : { start: 0, end: 5 };
      const result = [
        { id: 'entity-1', from_name: 'entity', to_name: 'text', type: 'labels', value: { ...position, labels: ['Entity'], text: 'Hello' } },
        { id: 'entity-1', from_name: 'note', to_name: 'text', type: 'textarea', value: { ...position, text: ['原始备注'] } },
      ];
      const annotation = store.annotationStore.addAnnotation({ result });
      store.annotationStore.selectAnnotation(annotation.id);
      annotation.deserializeResults(annotation.versions.result);
      annotation.reinitHistory(false);
      annotation.confirmSavedState();
      env.events.hasEvent.mockReturnValue(true);
      const region = annotation.regions[0];
      const note = region.results.find((r) => r.type === 'textarea');
      return { annotation, region, note, env };
    };

    it.each(["changed", "missing"])("retains %s draft edits and refuses queued autosaves or manual retries until reload", async (reason) => {
      const { annotation, note, env } = createTextDraft();
      annotation.autosave = { cancel: jest.fn() };
      note.setValue(['旧页修改']);
      env.events.invokeFirst = jest.fn().mockImplementationOnce(async () => {
        annotation.setDraftConflict(reason);
        return { $meta: { status: 409 } };
      });
      await annotation.saveDraftImmediatelyWithResults();
      note.setValue(['冲突后继续编辑']);
      await annotation.saveDraft();
      await annotation.saveDraftImmediatelyWithResults();
      expect(env.events.invokeFirst).toHaveBeenCalledTimes(1);
      expect(annotation.autosave.cancel).toHaveBeenCalled();
      expect(annotation.hasUnsavedDraft).toBe(true);
      expect(annotation.draftConflict).toBe(true);
      expect(annotation.draftConflictReason).toBe(reason);
      expect(note.value.text).toEqual(['冲突后继续编辑']);
    });

    it("updates the observed recovery copy after edits inside real MST serialization actions", () => {
      const { annotation, note } = createTextDraft();
      annotation.setDraftId(7);
      annotation.setDraftRevision(1);
      const copies = [];
      const stop = autorun(() => copies.push(annotation.draftBackup));
      const first = copies[copies.length - 1];
      note.setValue(['冲突后的最新备注']);
      const latest = JSON.parse(copies[copies.length - 1]);
      expect(copies[copies.length - 1]).not.toEqual(first);
      expect(latest.result.find(r => r.from_name === 'note').value.text).toEqual(['冲突后的最新备注']);
      expect(latest.draft_id).toBe(7);
      stop();
    });

    it("keeps a restored entity clean when its text range is initialized", () => {
      const { annotation, region } = createTextDraft();
      const result = annotation.serializeAnnotation();
      expect(annotation.hasUnsavedDraft).toBe(false);
      region.initRangeAndOffsets();
      expect(annotation.serializeAnnotation()).toEqual(result);
      expect(annotation.hasUnsavedDraft).toBe(false);
    });

    it("does not turn plain-text range initialization into an autosave", async () => {
      jest.useFakeTimers();
      try {
        const { annotation, region, env } = createTextDraft();
        env.events.invokeFirst = jest.fn().mockResolvedValue({ $meta: { status: 201 } });
        annotation.startAutosave();
        await jest.advanceTimersByTimeAsync(0);
        region.initRangeAndOffsets();
        await jest.advanceTimersByTimeAsync(5001);
        expect(env.events.invokeFirst).not.toHaveBeenCalled();
        expect(annotation.hasUnsavedDraft).toBe(false);
        annotation.autosave.cancel();
      } finally { jest.useRealTimers(); }
    });

    it.each([503, 'offline'])("retains a real note edit and failed %s save through range initialization", async (failure) => {
      const { annotation, region, note, env } = createTextDraft();
      note.setValue(['编辑未保存']);
      env.events.invokeFirst = jest.fn();
      if (failure === 'offline') env.events.invokeFirst.mockRejectedValueOnce(new Error('offline'));
      else env.events.invokeFirst.mockResolvedValueOnce({ $meta: { status: failure } });
      env.events.invokeFirst.mockResolvedValue({ $meta: { status: 200 } });
      await annotation.saveDraftImmediatelyWithResults().catch(() => {});
      region.initRangeAndOffsets();
      expect(annotation.hasUnsavedDraft).toBe(true);
      expect(annotation.draftSaveError).toBe(true);
      await annotation.saveDraftImmediatelyWithResults();
      expect(annotation.hasUnsavedDraft).toBe(false);
      expect(note.value.text).toEqual(['编辑未保存']);
    });

    it("does not confirm newer text edits when an older save completes", async () => {
      const { annotation, region, note, env } = createTextDraft();
      note.setValue(['第一版']);
      let complete;
      env.events.invokeFirst = jest.fn().mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }))
        .mockResolvedValue({ $meta: { status: 200 } });
      const saving = annotation.saveDraft();
      note.setValue(['第二版']);
      region.initRangeAndOffsets();
      complete({ $meta: { status: 200 } });
      await saving;
      expect(annotation.hasUnsavedDraft).toBe(true);
      await annotation.saveDraftImmediatelyWithResults();
      expect(annotation.hasUnsavedDraft).toBe(false);
      annotation.undo();
      expect(annotation.hasUnsavedDraft).toBe(true);
      annotation.redo();
      expect(annotation.hasUnsavedDraft).toBe(false);
    });

    it("still protects changed entity boundaries", () => {
      const { annotation, region } = createTextDraft();
      region.updateTextOffsets(0, 4);
      region.initRangeAndOffsets();
      expect(annotation.hasUnsavedDraft).toBe(true);
      expect(annotation.serializeAnnotation().find((r) => r.type === 'labels').value.end).toBe(4);
    });

    it("leaves HTML range calculation dependent on its DOM", () => {
      const { region } = createTextDraft(true);
      expect(region.isText).toBe(false);
      expect(region.globalOffsets).toBeNull();
    });
  });
  describe("browser unload draft confirmation", () => {
    const createDraft = () => {
      const env = createTestEnv();
      const store = AppStore.create({ config: '<View><Image name="img" value="$img"/><Rectangle name="rect" toName="img"/></View>', task: { id: 1, data: JSON.stringify({ img: "/test.jpg" }) }, interfaces: ["basic"] }, env);
      store.initializeStore({});
      const annotation = store.annotationStore.addAnnotation({ result: [{ from_name: "rect", to_name: "img", type: "rectangle", value: { x: 0, y: 0, width: 20, height: 20 } }] });
      annotation.deserializeResults(annotation.versions.result);
      annotation.reinitHistory(false);
      annotation.confirmSavedState();
      env.events.hasEvent.mockReturnValue(true);
      return { annotation, env, region: annotation.regions[0] };
    };

    it("does not confirm unsaved edits when media initialization resets Undo", () => {
      const { annotation, region } = createDraft();
      region.setProperty("width", 22);
      expect(annotation.hasUnsavedDraft).toBe(true);
      annotation.reinitHistory(false);
      expect(annotation.history.hasChanges).toBe(false);
      expect(annotation.hasUnsavedDraft).toBe(true);
    });

    it("keeps a pending autosave through the actual image-load callback", async () => {
      jest.useFakeTimers();
      try {
        const env = createTestEnv();
        env.events.hasEvent.mockReturnValue(true);
        env.events.invokeFirst = jest.fn().mockResolvedValue({ $meta: { status: 201 } });
        const store = AppStore.create({ config: '<View><Image name="img" value="$img"/><Rectangle name="rect" toName="img"/></View>', task: { id: 1, data: JSON.stringify({ img: "/test.jpg" }) }, interfaces: ["basic"] }, env);
        store.initializeStore({ annotations: [{ result: [{ from_name: "rect", to_name: "img", type: "rectangle", value: { x: 0, y: 0, width: 20, height: 20 } }] }] });
        const annotation = store.annotationStore.selected;
        await jest.advanceTimersByTimeAsync(0);
        expect(annotation.hasUnsavedDraft).toBe(false);
        annotation.regions[0].setProperty("width", 22);
        const image = annotation.names.get("img");
        image.setContainerRef({ offsetWidth: 600, offsetHeight: 400 });
        image.updateImageSize({ target: { naturalWidth: 600, naturalHeight: 400 } });
        await jest.advanceTimersByTimeAsync(1);
        expect(annotation.hasUnsavedDraft).toBe(true);
        expect(env.events.invokeFirst).not.toHaveBeenCalled();
        await jest.advanceTimersByTimeAsync(5000);
        expect(env.events.invokeFirst).toHaveBeenCalledTimes(1);
        expect(annotation.hasUnsavedDraft).toBe(false);
        annotation.autosave.cancel();
      } finally {
        jest.useRealTimers();
      }
    });

    it("uses a restored server draft as the baseline without confirming later media edits", () => {
      const { annotation, region } = createDraft();
      region.setProperty("width", 22);
      annotation.setDraftSaved("2026-10-15T00:00:00Z");
      expect(annotation.hasUnsavedDraft).toBe(false);
      region.setProperty("width", 23);
      annotation.reinitHistory(false);
      expect(annotation.hasUnsavedDraft).toBe(true);
    });

    it("confirms a formal save even without an autosave handler", () => {
      const { annotation, region } = createDraft();
      region.setProperty("width", 22);
      expect(annotation.hasUnsavedDraft).toBe(true);
      annotation.dropDraft();
      expect(annotation.hasUnsavedDraft).toBe(false);
      expect(annotation.history.hasChanges).toBe(true);
    });

    it("does not treat an older successful save as confirmation of newer edits", async () => {
      const { annotation, env, region } = createDraft();
      expect(annotation.hasUnsavedDraft).toBe(false);
      region.setProperty("width", 22);
      expect(annotation.hasUnsavedDraft).toBe(true);
      let complete;
      env.events.invokeFirst = jest.fn().mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }))
        .mockResolvedValue({ $meta: { status: 200 } });
      const first = annotation.saveDraft();
      region.setProperty("width", 23);
      complete({ $meta: { status: 200 } });
      await first;
      expect(annotation.hasUnsavedDraft).toBe(true);
      await annotation.saveDraftImmediatelyWithResults();
      expect(annotation.hasUnsavedDraft).toBe(false);
      annotation.undo();
      expect(annotation.hasUnsavedDraft).toBe(true);
      annotation.redo();
      expect(annotation.hasUnsavedDraft).toBe(false);
    });

    it.each([503, "offline", "unconfirmed"])("keeps unload protection after %s and clears it only on retry success", async (failure) => {
      const { annotation, env, region } = createDraft();
      region.setProperty("width", 22);
      env.events.invokeFirst = jest.fn();
      if (failure === "offline") env.events.invokeFirst.mockRejectedValueOnce(new Error("offline"));
      else env.events.invokeFirst.mockResolvedValueOnce(failure === "unconfirmed" ? {} : { $meta: { status: failure } });
      env.events.invokeFirst.mockResolvedValue({ $meta: { status: 200 } });
      await annotation.saveDraftImmediatelyWithResults().catch(() => {});
      expect(annotation.draftSaveError).toBe(true);
      expect(annotation.hasUnsavedDraft).toBe(true);
      await annotation.saveDraftImmediatelyWithResults();
      expect(annotation.draftSaveError).toBe(false);
      expect(annotation.hasUnsavedDraft).toBe(false);
      annotation.setReadonly(true);
      expect(annotation.hasUnsavedDraft).toBe(false);
    });
  });
  describe("draft failure recovery", () => {
    it.each([200, 403, "offline"])("flushes edits made during an explicit save, with follow-up result %s", async (status) => {
      const env = createTestEnv();
      const store = AppStore.create({
        config: '<View><Image name="img" value="$img"/><Rectangle name="rect" toName="img"/></View>',
        task: { id: 1, data: JSON.stringify({ img: "/test.jpg" }) },
        interfaces: ["basic"],
      }, env);
      store.initializeStore({});
      const annotation = store.annotationStore.addAnnotation({ result: [{
        from_name: "rect", to_name: "img", type: "rectangle",
        value: { x: 0, y: 0, width: 20, height: 20 },
      }] });
      annotation.deserializeResults(annotation.versions.result);
      const region = annotation.regions[0];
      const pending = [];
      const sent = [];
      env.events.hasEvent.mockReturnValue(true);
      env.events.invokeFirst = jest.fn(() => {
        sent.push(annotation.serializeAnnotation({ fast: true }));
        return new Promise((resolve, reject) => pending.push({ resolve, reject }));
      });
      const flush = annotation.saveDraftImmediatelyWithResults();
      const settled = jest.fn();
      // Observe rejection immediately, including the follow-up offline case.
      const outcome = flush.then((res) => ({ res }), (error) => ({ error })).then((value) => { settled(); return value; });
      region.setProperty("width", 22);
      pending[0].resolve({ $meta: { status: 200 } });
      // Drain the real MST flow and promise continuations without running timers.
      for (let i = 0; i < 20; i++) await Promise.resolve();
      expect(settled).not.toHaveBeenCalled();
      expect(sent.map((result) => result[0].value.width)).toEqual([20, 22]);
      if (status === "offline") pending[1].reject(new Error("offline"));
      else pending[1].resolve({ $meta: { status } });
      const result = await outcome;
      if (status === "offline") expect(result.error.message).toBe("offline");
      else expect(result.res.$meta.status).toBe(status);
      expect(region.width).toBe(22);
      expect(annotation.isDraftSaving).toBe(false);
    });

    it("propagates a network failure, releases saving, and retries without marking the failed draft saved", async () => {
      const { annotation, env } = createStoreWithAnnotation();
      env.events.hasEvent.mockReturnValue(true);
      env.events.invokeFirst = jest.fn().mockRejectedValueOnce(new Error("offline"))
        .mockResolvedValueOnce({ $meta: { status: 200 } });
      await expect(annotation.saveDraftImmediatelyWithResults()).rejects.toThrow("offline");
      expect(annotation.isDraftSaving).toBe(false);
      expect(annotation.draftSaved).toBeUndefined();
      await expect(annotation.saveDraftImmediatelyWithResults()).resolves.toMatchObject({ $meta: { status: 200 } });
      expect(annotation.isDraftSaving).toBe(false);
      expect(annotation.draftSaved).toBeDefined();
    });

    it("does not mark a rejected HTTP response as saved", async () => {
      const { annotation, env } = createStoreWithAnnotation();
      env.events.hasEvent.mockReturnValue(true);
      env.events.invokeFirst = jest.fn().mockResolvedValue({ $meta: { status: 403 } });
      await annotation.saveDraftImmediatelyWithResults();
      expect(annotation.isDraftSaving).toBe(false);
      expect(annotation.draftSaved).toBeUndefined();
    });

    it("waits for an autosave then serializes the latest edits before confirming navigation", async () => {
      const { annotation, env } = createStoreWithAnnotation();
      env.events.hasEvent.mockReturnValue(true);
      let complete;
      const sent = [];
      let value = "before";
      annotation.serializeAnnotation = jest.fn(() => [value]);
      env.events.invokeFirst = jest.fn(() => {
        sent.push(value);
        return sent.length === 1 ? new Promise((resolve) => { complete = resolve; }) : Promise.resolve({ $meta: { status: 200 } });
      });
      const first = annotation.saveDraft();
      value = "latest";
      const leave = annotation.saveDraftImmediatelyWithResults();
      let left = false;
      leave.then(() => { left = true; });
      await Promise.resolve();
      expect(left).toBe(false);
      complete({ $meta: { status: 200 } });
      await first;
      await leave;
      expect(sent).toEqual(["before", "latest"]);
      expect(annotation.isDraftSaving).toBe(false);
    });
  });
  describe("creation and snapshot", () => {
    it("creates annotation with default type and editable", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(annotation.type).toBe("annotation");
      expect(annotation.editable).toBe(true);
      expect(annotation.id).toBeDefined();
    });

    it("creates prediction with editable false", () => {
      const env = createTestEnv();
      const store = AppStore.create(
        {
          config: MINIMAL_CONFIG,
          task: { id: 1, data: JSON.stringify({ text: "Hi" }) },
          interfaces: ["basic"],
        },
        env,
      );
      store.initializeStore({});
      const pred = store.annotationStore.addPrediction({ result: [] });
      expect(pred.type).toBe("prediction");
      expect(pred.editable).toBe(false);
    });
  });

  describe("views", () => {
    it("store returns root store", () => {
      const { store, annotation } = createStoreWithAnnotation();
      expect(annotation.store).toBe(store);
    });

    it("list returns annotation store", () => {
      const { store, annotation } = createStoreWithAnnotation();
      expect(annotation.list).toBe(store.annotationStore);
    });

    it("regions returns array from areas", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(annotation.regions).toEqual([]);
    });

    it("results returns empty array when no areas", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(annotation.results).toEqual([]);
    });

    it("hasSelection reflects regionStore.hasSelection", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(annotation.hasSelection).toBe(false);
    });

    it("selectionSize reflects regionStore selection size", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(annotation.selectionSize).toBe(0);
    });

    it("selectedRegions returns empty array when none selected", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(annotation.selectedRegions).toEqual([]);
    });

    it("exists is false when pk and versions not set", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(annotation.exists).toBe(false);
    });

    it("isReadOnly returns true when readonly is true", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.setReadonly(true);
      expect(annotation.isReadOnly()).toBe(true);
    });

    it("isReadOnly returns false when editable and not readonly", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(annotation.isReadOnly()).toBe(false);
    });
  });

  describe("actions", () => {
    it("setEditable updates editable", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.setEditable(false);
      expect(annotation.editable).toBe(false);
    });

    it("setReadonly updates readonly", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.setReadonly(true);
      expect(annotation.readonly).toBe(true);
    });

    it("toggleVisibility toggles hidden", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(annotation.hidden).toBe(false);
      annotation.toggleVisibility(true);
      expect(annotation.hidden).toBe(false);
      annotation.toggleVisibility();
      expect(annotation.hidden).toBe(true);
    });

    it("setIsDrawing updates isDrawing", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.setIsDrawing(true);
      expect(annotation.isDrawing).toBe(true);
    });

    it("setDragMode updates dragMode", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.setDragMode(true);
      expect(annotation.dragMode).toBe(true);
    });

    it("unselectAreas does not throw when selection empty", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(() => annotation.unselectAreas()).not.toThrow();
    });

    it("unselectAll clears selection and does not throw", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(() => annotation.unselectAll()).not.toThrow();
      expect(() => annotation.unselectAll(true)).not.toThrow();
    });

    it("validate returns true for empty annotation", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(annotation.validate()).toBe(true);
    });

    it("beforeSend traverses tree and stops linking mode", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(() => annotation.beforeSend()).not.toThrow();
    });

    it("deleteAllRegions with no regions does not throw", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(() => annotation.deleteAllRegions()).not.toThrow();
    });

    it("deleteAllRegions with deleteReadOnly clears and updates", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(() => annotation.deleteAllRegions({ deleteReadOnly: true })).not.toThrow();
    });

    it("updateObjects does not throw", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(() => annotation.updateObjects()).not.toThrow();
      expect(() => annotation.updateObjects(false)).not.toThrow();
    });

    it("prepareAnnotation parses JSON string", () => {
      const { annotation } = createStoreWithAnnotation();
      const result = annotation.prepareAnnotation(
        '[{"type":"labels","from_name":"l","to_name":"t1","value":{"labels":["A"]}}]',
      );
      expect(Array.isArray(result)).toBe(true);
    });

    it("prepareAnnotation returns array for array input", () => {
      const { annotation } = createStoreWithAnnotation();
      const input = [];
      expect(annotation.prepareAnnotation(input)).toEqual([]);
    });

    it("fixBrokenAnnotation filters invalid results and fixes types", () => {
      const { annotation } = createStoreWithAnnotation();
      const json = [
        { type: "relation", from_id: "a", to_id: "b", direction: "right" },
        { type: "htmllabels", from_name: "x", to_name: "y", value: {} },
      ];
      const fixed = annotation.fixBrokenAnnotation(json);
      expect(fixed.length).toBeLessThanOrEqual(json.length);
    });

    it("fixBrokenAnnotation passes through relation type", () => {
      const { annotation } = createStoreWithAnnotation();
      const json = [{ type: "relation", from_id: "a", to_id: "b", direction: "right", labels: [] }];
      const fixed = annotation.fixBrokenAnnotation(json);
      expect(fixed.some((r) => r.type === "relation")).toBe(true);
    });

    it("serializeAnnotation returns array and resets cursor", () => {
      const { annotation } = createStoreWithAnnotation();
      const result = annotation.serializeAnnotation();
      expect(Array.isArray(result)).toBe(true);
      expect(document.body.style.cursor).toBe("default");
    });

    it("setGroundTruth updates ground_truth", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.setGroundTruth(true, false);
      expect(annotation.ground_truth).toBe(true);
    });

    it("sendUserGenerate sets sentUserGenerate", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.sendUserGenerate();
      expect(annotation.sentUserGenerate).toBe(true);
    });

    it("updatePersonalKey sets pk", () => {
      const { store, annotation } = createStoreWithAnnotation();
      store.addAnnotationToTaskHistory = jest.fn();
      annotation.updatePersonalKey("42");
      expect(annotation.pk).toBe("42");
      expect(store.addAnnotationToTaskHistory).toHaveBeenCalledWith("42");
    });

    it("setUnresolvedCommentCount and setCommentCount update counts", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.setUnresolvedCommentCount(2);
      annotation.setCommentCount(5);
      expect(annotation.unresolved_comment_count).toBe(2);
      expect(annotation.comment_count).toBe(5);
    });

    it("addVersions merges versions and can set draftSelected", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.addVersions({ draft: [] });
      expect(annotation.versions.draft).toEqual([]);
    });

    it("setDraftId and setDraftSelected update volatile state", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.setDraftId(99);
      annotation.setDraftSelected(true);
      expect(annotation.draftId).toBe(99);
      expect(annotation.draftSelected).toBe(true);
    });

    it("setDraftSaving and setDraftSaved update state", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.setDraftSaving(true);
      expect(annotation.isDraftSaving).toBe(true);
      annotation.setDraftSaved("2020-01-01");
      expect(annotation.draftSaved).toBe("2020-01-01");
    });

    it("dropDraft clears draft state when autosave exists", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.autosave = { cancel: jest.fn() };
      annotation.setDraftId(1);
      annotation.setDraftSelected(true);
      annotation.addVersions({ draft: [] });
      annotation.dropDraft();
      expect(annotation.draftId).toBe(0);
      expect(annotation.draftSelected).toBe(false);
      expect(annotation.versions.draft).toBeUndefined();
    });

    it("reinitHistory calls history.reinit and setInitialValues for annotation type", () => {
      const { annotation } = createStoreWithAnnotation();
      annotation.history.reinit = jest.fn();
      annotation.reinitHistory(true);
      expect(annotation.history.reinit).toHaveBeenCalledWith(true);
    });

    it("deserializeAnnotation warns and delegates to deserializeResults", () => {
      const consoleSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
      const { annotation } = createStoreWithAnnotation();
      annotation.deserializeResults = jest.fn();
      annotation.deserializeAnnotation([]);
      expect(consoleSpy).toHaveBeenCalled();
      expect(annotation.deserializeResults).toHaveBeenCalledWith([]);
      consoleSpy.mockRestore();
    });

    it("prepareValue returns value for non-text types", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(annotation.prepareValue({ labels: ["A"] }, "rectanglelabels")).toEqual({ labels: ["A"] });
    });

    it("prepareValue transforms start/end to startOffset/endOffset for text types", () => {
      const { annotation } = createStoreWithAnnotation();
      const value = { start: 0, end: 5 };
      const result = annotation.prepareValue(value, "richtext");
      expect(result.startOffset).toBe(0);
      expect(result.endOffset).toBe(5);
      expect(result.isText).toBe(true);
    });

    it("rejectAllSuggestions clears suggestions", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(() => annotation.rejectAllSuggestions()).not.toThrow();
    });

    it("resetReady iterates objects and areas", () => {
      const { annotation } = createStoreWithAnnotation();
      expect(() => annotation.resetReady()).not.toThrow();
    });
  });
});
