import { LabelStudio } from "./LabelStudio";
import { configureStore } from "./configureStore";
import { destroy } from "mobx-state-tree";
import { createRoot } from "react-dom/client";

jest.mock("./configureStore", () => ({ configureStore: jest.fn() }));
jest.mock("mobx-state-tree", () => ({ ...jest.requireActual("mobx-state-tree"), destroy: jest.fn() }));
jest.mock("./Component", () => ({ LabelStudio: () => null }));
jest.mock("./EditorLocaleRoot", () => ({ EditorLocaleRoot: () => null }));
jest.mock("react-dom/client", () => ({ createRoot: jest.fn() }));
jest.mock("./mixins/SharedChoiceStore/mixin", () => ({ destroy: jest.fn() }));
jest.mock("./core/Hotkey", () => ({ Hotkey: { setKeymap: jest.fn(), unbindAll: jest.fn() } }));
jest.mock("./utils/feature-flags", () => ({ isFF: () => false, FF_LSDV_4620_3_ML: "test" }));

const makeStore = () => ({ setAppControls: jest.fn(), submitAnnotation: jest.fn(), saveDraft: jest.fn() });
const nextTick = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  jest.clearAllMocks();
  (createRoot as jest.Mock).mockReturnValue({ render: jest.fn(), unmount: jest.fn() });
});

it("keeps a mounted editor and its store while changing its locale", async () => {
  const store = makeStore();
  (configureStore as jest.Mock).mockResolvedValue({ store });
  const editor = new LabelStudio(document.createElement("div"), {
    interfaces: [],
    locale: "unsupported" as any,
    instanceOptions: { reactVersion: "v18" },
  });
  await nextTick();
  expect(editor.localeRuntime.locale).toBe("en-US");
  expect(createRoot).toHaveBeenCalledTimes(1);
  expect(editor.setLocale("zh-CN")).toBe(true);
  expect(editor.localeRuntime.t("editor:submit")).toBe("提交");
  expect(configureStore).toHaveBeenCalledTimes(1);
  expect(configureStore).toHaveBeenCalledWith(expect.anything(), expect.anything(), editor.localeRuntime);
  expect(store.submitAnnotation).not.toHaveBeenCalled();
  expect(store.saveDraft).not.toHaveBeenCalled();
  editor.destroy?.();
  expect(destroy).toHaveBeenCalledWith(store);
  expect(editor.setLocale("en-US")).toBe(false);
});

it("does not mount an editor after its async setup is cancelled", async () => {
  let resolveStore!: (value: unknown) => void;
  (configureStore as jest.Mock).mockReturnValue(new Promise((resolve) => { resolveStore = resolve; }));
  const store = makeStore();
  const editor = new LabelStudio(document.createElement("div"), {
    interfaces: [],
    locale: "zh-CN",
    instanceOptions: { reactVersion: "v18" },
  });
  editor.destroy?.();
  resolveStore({ store });
  await nextTick();
  expect(createRoot).not.toHaveBeenCalled();
  expect(destroy).toHaveBeenCalledWith(store);
});
