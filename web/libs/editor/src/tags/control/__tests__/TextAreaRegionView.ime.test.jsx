import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Registry from "../../../core/Registry";
import "../TextArea/TextAreaRegionView";
import { createLocaleRuntime } from "@humansignal/i18n";

let runtime;
beforeEach(() => { runtime = createLocaleRuntime("en-US"); });
afterEach(() => { runtime.destroy(); });

jest.mock("mobx-state-tree", () => ({ ...jest.requireActual("mobx-state-tree"), isAlive: () => true }));

const RegionView = Registry.getPerRegionView("textarea", "region-list");

function setup(rows, existing = false) {
  const item = {
    rows,
    _value: "中文候选",
    mode: "region-list",
    allowsubmit: true,
    annotation: { isReadOnly: () => false, setPendingTextInput: jest.fn() },
    isReadOnly: () => false,
    editable: true,
    isDeleteable: true,
    setValue: jest.fn(),
    addText: jest.fn(),
    addTextToResult: jest.fn(),
  };
  const result = {
    mainValue: ["原始备注"],
    from_name: item,
    isReadOnly: () => false,
    setValue: jest.fn(),
  };
  result.mainValue.toJSON = () => ["原始备注"];
  const area = { results: existing ? [result] : [], isReadOnly: () => false, isSelected: true };
  result.area = area;
  item.perRegionArea = area;
  const Provider = runtime.provider;
  const view = render(<Provider><RegionView item={item} area={area} collapsed={false} /></Provider>);
  return { item, result, view };
}

it("protects a local region edit before blur and releases its registration when unmounted", () => {
  const { item, result, view } = setup(1, true);
  fireEvent.change(screen.getAllByTestId('textarea-region-input')[0], { target: { value: '尚未确认的区域备注' } });
  const [key, pending] = item.annotation.setPendingTextInput.mock.calls.at(-1);
  expect(pending).toBe(true);
  expect(result.setValue).not.toHaveBeenCalled();
  view.unmount();
  expect(item.annotation.setPendingTextInput).toHaveBeenLastCalledWith(key, false);
});

it.each([1, 3])("keeps per-region composition out of submission with rows=%s", async (rows) => {
  const { item } = setup(rows);
  const input = screen.getByTestId("textarea-region-input");
  input.focus();
  const event = new KeyboardEvent("keydown", { key: "Enter", isComposing: true, bubbles: true, cancelable: true });
  act(() => input.dispatchEvent(event));
  expect(event.defaultPrevented).toBe(true);
  expect(input).toHaveFocus();
  expect(item.addText).not.toHaveBeenCalled();
  if (rows === 1) fireEvent.submit(screen.getByTestId("textarea-region-form"));
  else fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(item.addText).toHaveBeenCalledWith("中文候选"));
});

it.each(["Enter", "Escape"])("keeps an existing region result focused during composing %s", (key) => {
  const { result } = setup(1, true);
  const input = screen.getAllByTestId("textarea-region-input")[0];
  input.focus();
  fireEvent.change(input, { target: { value: "修改候选" } });
  const event = new KeyboardEvent("keydown", { key, isComposing: true, bubbles: true, cancelable: true });
  act(() => input.dispatchEvent(event));
  if (key === "Enter") expect(event.defaultPrevented).toBe(true);
  expect(input).toHaveFocus();
  expect(input).toHaveValue("修改候选");
  expect(result.setValue).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: "Enter" });
  expect(input).not.toHaveFocus();
  expect(result.setValue).toHaveBeenCalledWith(["修改候选"]);
});
