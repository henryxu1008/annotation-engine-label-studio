import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Labeling } from "./Label";

let mockStore;
jest.mock("mobx-react", () => ({ inject: () => (Component) => (props) => <Component {...props} store={mockStore} loading={false} /> }));
jest.mock("mobx-react-lite", () => ({ observer: (component) => component }));
jest.mock("@humansignal/i18n", () => ({ useLocaleTranslation: () => ({ t: (key) => key }) }));
jest.mock("@humansignal/ui", () => ({ Button: ({ children, ...props }) => <button {...props}>{children}</button> }));
jest.mock("@humansignal/icons", () => ({ IconChevronDown: () => null, IconChevronLeft: () => null, IconGearNewUI: () => null }));
jest.mock("../Common/FieldsButton", () => ({ FieldsButton: () => <div>Fields</div> }));
jest.mock("../Common/Icon/Icon", () => ({ Icon: () => null }));
jest.mock("../Common/Resizer/Resizer", () => ({ Resizer: ({ children }) => <div>{children}</div> }));
jest.mock("../Common/Space/Space", () => ({ Space: ({ children }) => <div>{children}</div> }));
jest.mock("../MainView", () => ({ DataView: () => <div>Legacy table</div> }));

it("opens restricted labeling without a saved view and offers a way back", async () => {
  const sdk = { mode: "labeling", settings: { blindProtocol: true }, interfaceEnabled: () => false,
    on: jest.fn(), off: jest.fn(), destroyLSF: jest.fn() };
  mockStore = { SDK: sdk, currentView: undefined, isExplorerMode: true, dataStore: { selected: null },
    closeLabeling: jest.fn().mockResolvedValue(true) };
  const { unmount } = render(<Labeling />);
  expect(screen.queryByText("Legacy table")).toBeNull();
  expect(screen.queryByText("Fields")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "back" }));
  await waitFor(() => expect(mockStore.closeLabeling).toHaveBeenCalledTimes(1));
  unmount();
  expect(sdk.destroyLSF).toHaveBeenCalled();
});
