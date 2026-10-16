import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import Registry from "../../../core/Registry";
import "../TextArea/TextAreaRegionView";

jest.mock("mobx-state-tree", () => ({ ...jest.requireActual("mobx-state-tree"), isAlive: () => true }));

const RegionView = Registry.getPerRegionView("textarea", "region-list");

function setup(rows, existing = false) {
  const item = {
    rows,
    _value: "中文候选",
    mode: "region-list",
    allowsubmit: true,
    annotation: { isReadOnly: () => false },
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
  render(<RegionView item={item} area={area} collapsed={false} />);
  return { item, result };
}

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
