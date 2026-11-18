import { fireEvent, render, screen } from "@testing-library/react";
import en from "../../../../../libs/i18n/src/catalogs/en-US/projects.json";
import zh from "../../../../../libs/i18n/src/catalogs/zh-CN/projects.json";
import { LifecycleSettings } from "./LifecycleSettings";

const mockCallApi = jest.fn();
let mockLocale = "en-US";
const mockCatalog = { "en-US": en, "zh-CN": zh };
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("../../providers/ProjectProvider", () => ({ useProject: () => ({ project: { id: 4 } }) }));
jest.mock("@humansignal/i18n", () => ({
  useLocaleTranslation: () => ({ t: (key) => mockCatalog[mockLocale][key] ?? key }),
}));
jest.mock("@humansignal/ui", () => ({ Button: ({ children, ...props }) => <button {...props}>{children}</button> }));

const counts = {
  exports: 0,
  conversions: 0,
  releases: 0,
  release_attempts: 0,
  snapshots: 0,
  audit_packages: 0,
  audit_attempts: 0,
  storage_local_import: 0,
  storage_local_export: 0,
  storage_redis_import: 0,
  storage_redis_export: 0,
  storage_s3_import: 0,
  storage_s3_export: 0,
  storage_gcs_import: 0,
  storage_gcs_export: 0,
  storage_azure_import: 0,
  storage_azure_export: 0,
};

beforeEach(() => {
  mockCallApi.mockReset();
  let state = "active";
  mockCallApi.mockImplementation(async (method) => {
    if (method === "projectLifecycle") return { state, revision: state === "active" ? 3 : 4 };
    if (method === "projectStorageOperations")
      return {
        operations: [],
        possibly_truncated: false,
        operations_next_cursor: null,
        legacy_export_blockers: counts,
        legacy_export_details: { items: [], possibly_truncated: false, next_cursor: null },
      };
    if (method === "projectLifecycleTransition") {
      state = "paused";
      return { state, revision: 4 };
    }
    throw new Error(method);
  });
});

it.each([
  ["en-US", "Project lifecycle", "New state", "Reason", "Change state", "Paused"],
  ["zh-CN", "项目生命周期", "目标状态", "原因", "更改状态", "已暂停"],
])("renders and submits the lifecycle form in %s", async (locale, title, target, reason, action, paused) => {
  mockLocale = locale;
  render(<LifecycleSettings />);
  await screen.findByRole("heading", { name: title });
  fireEvent.change(await screen.findByLabelText(target), { target: { value: "paused" } });
  fireEvent.change(screen.getByLabelText(reason), { target: { value: "synthetic maintenance" } });
  fireEvent.click(screen.getByRole("button", { name: action }));
  expect(mockCallApi).toHaveBeenCalledWith(
    "projectLifecycleTransition",
    expect.objectContaining({
      body: { target: "paused", expected_revision: 3, reason: "synthetic maintenance" },
    }),
  );
  expect(await screen.findByText(paused, { selector: "strong" })).toBeTruthy();
});
