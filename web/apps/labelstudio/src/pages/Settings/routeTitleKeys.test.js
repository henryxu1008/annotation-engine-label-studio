import { settingsSectionTitleKeyForRoute } from "./routeTitleKeys";

describe("project settings route titles", () => {
  it("maps fixed child routes to the same locale keys as the settings menu", () => {
    expect(settingsSectionTitleKeyForRoute("/projects/:id(\\d+)/settings/danger-zone")).toBe("dangerZone");
    expect(settingsSectionTitleKeyForRoute("/projects/:id(\\d+)/settings/members")).toBe("members");
    expect(settingsSectionTitleKeyForRoute("/projects/:id(\\d+)/settings/lifecycle")).toBe("lifecycleTitle");
    expect(settingsSectionTitleKeyForRoute("/projects/:id(\\d+)/settings/")).toBe("general");
  });

  it("leaves project titles and unknown paths to their original owner", () => {
    expect(settingsSectionTitleKeyForRoute("/projects/:id(\\d+)/data")).toBeNull();
    expect(settingsSectionTitleKeyForRoute("/projects/:id(\\d+)/settings/custom")).toBeNull();
  });
});
