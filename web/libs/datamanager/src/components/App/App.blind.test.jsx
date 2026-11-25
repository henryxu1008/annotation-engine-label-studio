import React from "react";
import { render, screen } from "@testing-library/react";
import { App } from "./App";

jest.mock("mobx-react", () => ({ observer: (component) => component, Provider: ({ children }) => children }));
jest.mock("../../providers/SDKProvider", () => ({ SDKProvider: ({ children }) => children }));
jest.mock("@tanstack/react-query", () => ({ QueryClientProvider: ({ children }) => children }));
jest.mock("@humansignal/core/lib/utils/query-client", () => ({ queryClient: {} }));
jest.mock("@humansignal/core/providers/AuthProvider", () => ({ AuthProvider: ({ children }) => children }));
jest.mock("@humansignal/i18n", () => ({ useLocaleTranslation: () => ({ t: (key) => key }) }));
jest.mock("../DataManager/DataManager", () => ({ DataManager: () => <div>Legacy table</div> }));
jest.mock("../Label/Label", () => ({ Labeling: () => <div>Editor</div> }));
jest.mock("../Common/Spinner", () => ({ Spinner: () => <div>Loading</div> }));

const app = (settings, blindWorkspace) => ({
  SDK: { mode: "explorer", settings, blindWorkspace }, crashed: false, loading: false, isLabeling: false,
});

it("renders the supplied restricted work flow instead of the legacy table", () => {
  const store = app({ blindProtocol: true }, ({ store }) => <div>Bound {store.SDK.mode}</div>);
  render(<App app={store} />);
  expect(screen.getByText("Bound explorer")).not.toBeNull();
  expect(screen.queryByText("Legacy table")).toBeNull();
});

it("fails closed when the restricted workspace is unavailable", () => {
  render(<App app={app({ blindProtocol: true }, null)} />);
  expect(screen.getByRole("alert").textContent).toBe("myWorkLoadFailed");
  expect(screen.queryByText("Legacy table")).toBeNull();
});

it("keeps the existing editor and ordinary table paths", () => {
  const store = app({ blindProtocol: false }, null);
  const { rerender } = render(<App app={store} />);
  expect(screen.getByText("Legacy table")).not.toBeNull();
  rerender(<App app={{ ...store, isLabeling: true, SDK: { ...store.SDK, settings: { blindProtocol: true } } }} />);
  expect(screen.getByText("Editor")).not.toBeNull();
});
