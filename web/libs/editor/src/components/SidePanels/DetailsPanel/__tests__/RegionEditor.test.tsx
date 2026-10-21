import { act, fireEvent, render, screen } from "@testing-library/react";
import { onAction, types } from "mobx-state-tree";
import { useLayoutEffect } from "react";
import { EditableRegion } from "../../../../regions/EditableRegion";
import { RegionEditor } from "../RegionEditor";

jest.mock("@humansignal/icons", () => ({ IconPropertyAngle: () => null }));
jest.mock("@humansignal/ui", () => ({ Checkbox: () => null, Select: () => null }));
jest.mock("../../../TimeDurationControl/TimeDurationControl", () => ({ TimeDurationControl: () => null }));
jest.mock("../TimelineRegionEditor", () => ({ TimelineRegionEditor: () => null }));

const Region = types
  .compose(EditableRegion, types.model({ type: "rectangleregion", width: 14.3 }))
  .volatile(() => ({ editableFields: [{ property: "width", label: "W" }] }))
  .views(() => ({ isReadOnly: () => false }));

function inputWidth(value: string) {
  const input = screen.getByRole("textbox", { name: "W", exact: true }) as HTMLInputElement;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("RegionEditor numeric properties", () => {
  it("keeps an edit delivered before passive mount effects instead of writing the initial value back", () => {
    const region = Region.create();
    const changes: number[] = [];
    const stop = onAction(region, (action) => {
      if (action.name === "setProperty") changes.push(action.args[1]);
    });

    function FirstPaintEdit() {
      // Reproduce selecting a region then filling its newly mounted input before
      // passive effects. A second click or an act() around each step masks this race.
      useLayoutEffect(() => inputWidth("16"), []);
      return <RegionEditor region={region as any} />;
    }

    render(<FirstPaintEdit />);
    expect(region.width).toBe(16);
    expect(screen.getByRole("textbox", { name: "W", exact: true })).toHaveValue("16");
    expect(changes).toEqual([16]);
    stop();
  });

  it("syncs external changes without calling the model setter again", () => {
    const region = Region.create();
    const changes: number[] = [];
    const stop = onAction(region, (action) => {
      if (action.name === "setProperty") changes.push(action.args[1]);
    });
    render(<RegionEditor region={region as any} />);
    expect(changes).toEqual([]);
    act(() => region.setProperty("width", 18));
    expect(screen.getByRole("textbox", { name: "W", exact: true })).toHaveValue("18");
    expect(changes).toEqual([18]);
    stop();
  });

  it("retains incomplete decimal input, then commits a complete decimal and keyboard increments", () => {
    const region = Region.create();
    render(<RegionEditor region={region as any} />);
    const input = screen.getByRole("textbox", { name: "W", exact: true });
    fireEvent.change(input, { target: { value: "16." } });
    expect(input).toHaveValue("16.");
    expect(region.width).toBe(14.3);
    fireEvent.change(input, { target: { value: "16.5" } });
    expect(region.width).toBe(16.5);
    fireEvent.keyDown(input, { key: "ArrowUp", shiftKey: true });
    expect(region.width).toBe(26.5);
    expect(input).toHaveValue("26.5");
  });
});
