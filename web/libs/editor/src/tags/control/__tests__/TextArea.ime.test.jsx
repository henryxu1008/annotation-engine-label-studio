import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { observable, runInAction } from "mobx";
import { HtxTextArea } from "../TextArea/TextArea";
import { TextArea } from "../../../common/TextArea/TextArea";

jest.mock("../../../core/Tree", () => ({
  ...jest.requireActual("../../../core/Tree").default,
  renderChildren: () => null,
}));

describe("text annotation composition", () => {
  function renderControl(rows) {
    const item = observable({
      name: "note",
      rows,
      _value: "中文候选",
      displaymode: "tag",
      regions: [],
      children: [],
      annotation: { isReadOnly: () => false },
      isReadOnly: () => false,
      perRegionVisible: () => true,
      showSubmit: true,
      allowsubmit: true,
      countTime: jest.fn(),
      setLastFocusedElement: jest.fn(),
      addText: jest.fn(),
      setValue: jest.fn((value) =>
        runInAction(() => {
          item._value = value;
        }),
      ),
    });
    render(<HtxTextArea item={item} />);
    return item;
  }

  it.each([1, 3])("does not submit or clear composing Enter with rows=%s", async (rows) => {
    const item = renderControl(rows);
    const input = screen.getByTestId("textarea-input");
    input.focus();
    const event = new KeyboardEvent("keydown", {
      key: "Enter",
      keyCode: 13,
      shiftKey: rows > 1,
      isComposing: true,
      bubbles: true,
      cancelable: true,
    });
    act(() => input.dispatchEvent(event));
    expect(event.defaultPrevented).toBe(true);
    expect(input).toHaveFocus();
    expect(input).toHaveValue("中文候选");
    expect(item.addText).not.toHaveBeenCalled();
    if (rows > 1) fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    else fireEvent.submit(screen.getByTestId("textarea-form"));
    await waitFor(() => expect(item.addText).toHaveBeenCalledTimes(1));
    expect(item.addText).toHaveBeenCalledWith("中文候选");
  });

  it("does not publish composing Ctrl+Enter in the common text editor", () => {
    const onSubmit = jest.fn();
    render(<TextArea value="中文候选" onSubmit={onSubmit} />);
    const input = screen.getByTestId("textarea-input");
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true, isComposing: true });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
    expect(onSubmit).toHaveBeenCalledWith("中文候选");
  });
});
