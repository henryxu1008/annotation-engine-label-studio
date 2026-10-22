import { selectedTaskPackageIds } from "./taskPackageSelection";

describe("task package selection ID domain", () => {
  it("copies explicitly selected task IDs without keeping mutable selection state", () => {
    const included = [9, 10];
    const ids = selectedTaskPackageIds({ target: "tasks", currentSelection: { all: false, included } });
    included.splice(0, 1);
    expect(ids).toEqual([9, 10]);
  });

  it.each(["annotations", "unknown", undefined])("ignores %s view IDs even when they overlap task IDs", (target) => {
    expect(selectedTaskPackageIds({ target, currentSelection: { all: false, included: [9, 10] } })).toEqual([]);
  });

  it("does not expand select-all, excluded IDs or uninitialized selection", () => {
    expect(selectedTaskPackageIds({ target: "tasks", currentSelection: { all: true, excluded: [9] } })).toEqual([]);
    expect(selectedTaskPackageIds({ target: "tasks" })).toEqual([]);
    expect(selectedTaskPackageIds(null)).toEqual([]);
  });
});
