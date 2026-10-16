import keymaster from "keymaster";
import "../Hotkey";

jest.mock("keymaster", () => {
  let scope = "__main__";
  return Object.assign(jest.fn(), {
    getScope: () => scope,
    setScope: (value: string) => {
      scope = value;
    },
  });
});

describe("annotation hotkey input isolation", () => {
  beforeEach(() => keymaster.setScope("__main__"));

  function filter(target: HTMLElement, init: KeyboardEventInit) {
    const event = new KeyboardEvent("keydown", { bubbles: true, ...init });
    Object.defineProperty(event, "target", { value: target });
    return keymaster.filter(event);
  }

  it.each(["input", "textarea", "select"])("does not forward numpad typing in %s", (tag) => {
    const listener = jest.fn();
    document.addEventListener("keydown", listener);
    try {
      expect(filter(document.createElement(tag), { key: "3", keyCode: 99 })).toBe(true);
      expect(keymaster.getScope()).toBe("__input__");
      expect(listener).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener("keydown", listener);
    }
  });

  it.each([{ isComposing: true, keyCode: 13 }, { keyCode: 229 }])("ignores composition keys %j", (init) => {
    expect(filter(document.createElement("input"), { key: "Enter", ...init })).toBe(false);
  });

  it.each(["checkbox", "radio"])("keeps shortcuts available while a %s has keyboard focus", (type) => {
    const input = document.createElement("input");
    input.type = type;
    keymaster.setScope("__input__");
    expect(filter(input, { key: "3", keyCode: 51 })).toBe(true);
    expect(keymaster.getScope()).toBe("__main__");
  });

  it("still forwards numpad label shortcuts outside text inputs", () => {
    const listener = jest.fn();
    document.addEventListener("keydown", listener);
    try {
      filter(document.body, { key: "3", keyCode: 99 });
      expect(listener).toHaveBeenCalledTimes(1);
      expect(listener.mock.calls[0][0].keyCode).toBe(51);
    } finally {
      document.removeEventListener("keydown", listener);
    }
  });
});
