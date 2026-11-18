jest.mock("./Tabs", () => {
  const { types } = jest.requireActual("mobx-state-tree");
  return {
    TabStore: types.model({ views: types.array(types.frozen()) }).views(() => ({
      get selected() {
        return null;
      },
    })),
  };
});

window.APP_SETTINGS = { hostname: "http://localhost" };
const { AppStore } = require("./AppStore");

it("returns no selection before a data manager view has loaded", () => {
  const store = AppStore.create({ toolbar: "" });
  expect(store.currentView).toBeNull();
  expect(store.currentSelection).toBeNull();
});
