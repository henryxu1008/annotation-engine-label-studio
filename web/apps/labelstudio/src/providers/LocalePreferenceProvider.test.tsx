import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime, useLocalePreference } from "@humansignal/i18n";
import { LanguagePreferences } from "@humansignal/app-common/pages/AccountSettings/sections/LanguagePreferences";
import { LocalePreferenceProvider } from "./LocalePreferenceProvider";

let mockAuthState: { user: { id: number } | null; isLoading: boolean };
const mockCallApi = jest.fn();

jest.mock("@humansignal/core/providers/AuthProvider", () => ({
  useAuth: () => mockAuthState,
}));
jest.mock("./ApiProvider", () => ({
  useAPI: () => ({ callApi: mockCallApi }),
}));

const response = (preference: string | null, resolvedLocale: string, source = "user") => ({
  preference,
  resolvedLocale,
  source,
  $meta: { ok: true },
});

function Probe() {
  const { preference, resolvedLocale, saving, error, setPreference } = useLocalePreference();
  return <>
    <div data-testid="locale-state">{`${preference ?? "auto"}/${resolvedLocale}/${saving}/${error ?? "none"}`}</div>
    <input aria-label="Unsubmitted project name" defaultValue="Original project" />
    <button onClick={() => void setPreference("zh-CN")}>Chinese</button>
    <button onClick={() => void setPreference(null)}>Automatic</button>
    <LanguagePreferences compact />
    <LanguagePreferences />
  </>;
}

describe("main app locale preference", () => {
  beforeEach(() => {
    mockCallApi.mockReset();
    mockAuthState = { user: { id: 7 }, isLoading: false };
    (window as any).APP_SETTINGS = {
      user: { id: 7 },
      locale: { resolvedLocale: "en-US", userPreference: null, source: "fallback" },
    };
    document.head.innerHTML = '<meta name="csrf-token" content="test-csrf">';
  });

  function mount() {
    const runtime = createLocaleRuntime("en-US");
    const Provider = runtime.provider;
    const view = render(<Provider><LocalePreferenceProvider><Probe /></LocalePreferenceProvider></Provider>);
    return { runtime, ...view, rerenderApp: () => view.rerender(
      <Provider><LocalePreferenceProvider><Probe /></LocalePreferenceProvider></Provider>,
    ) };
  }

  it("applies only a confirmed save, preserves typed form data, and clears explicit choice", async () => {
    mockCallApi.mockResolvedValueOnce(response(null, "en-US", "fallback"));
    const { runtime, unmount } = mount();
    const input = screen.getByRole("textbox", { name: "Unsubmitted project name" }) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "My draft" } });
    await waitFor(() => expect(mockCallApi).toHaveBeenCalledWith("currentUserLocale", { suppressError: true }));

    mockCallApi.mockResolvedValueOnce({ $meta: { ok: false } });
    fireEvent.click(screen.getByRole("button", { name: "Chinese" }));
    await waitFor(() => expect(screen.getByTestId("locale-state").textContent).toBe("auto/en-US/false/saveFailed"));
    expect(runtime.locale).toBe("en-US");
    expect(input.value).toBe("My draft");

    mockCallApi.mockResolvedValueOnce(response("zh-CN", "zh-CN"));
    fireEvent.click(screen.getByRole("button", { name: "Chinese" }));
    await waitFor(() => expect(runtime.locale).toBe("zh-CN"));
    expect(document.documentElement.lang).toBe("zh-CN");
    expect(input.value).toBe("My draft");
    expect(mockCallApi).toHaveBeenCalledWith("updateCurrentUserLocale", expect.objectContaining({
      body: { preference: "zh-CN" }, headers: { "X-CSRFToken": "test-csrf" }, suppressError: true,
    }));

    mockCallApi.mockResolvedValueOnce(response(null, "en-US", "fallback"));
    fireEvent.click(screen.getByRole("button", { name: "Automatic" }));
    await waitFor(() => expect(screen.getByTestId("locale-state").textContent).toBe("auto/en-US/false/none"));
    expect(input.value).toBe("My draft");
    unmount();
    runtime.destroy();
  });

  it("discards a late response from the previous account", async () => {
    let resolveOld!: (value: any) => void;
    mockCallApi.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
    const { runtime, unmount, rerenderApp } = mount();
    await waitFor(() => expect(mockCallApi).toHaveBeenCalledTimes(1));
    mockAuthState = { user: { id: 8 }, isLoading: false };
    mockCallApi.mockResolvedValueOnce(response(null, "en-US", "fallback"));
    rerenderApp();
    resolveOld(response("zh-CN", "zh-CN"));
    await waitFor(() => expect(mockCallApi).toHaveBeenCalledTimes(2));
    expect(runtime.locale).toBe("en-US");
    expect(screen.getByTestId("locale-state").textContent).toBe("auto/en-US/false/none");
    unmount();
    runtime.destroy();
  });

  it("waits for the authenticated actor before enabling either language selector", async () => {
    // The SSR avatar is present before Whoami confirms the session's actor.
    mockAuthState = { user: null, isLoading: true };
    const { runtime, unmount, rerenderApp } = mount();
    const menu = screen.getByTestId("menu-language-select") as HTMLSelectElement;
    const account = screen.getByTestId("language-preference-select") as HTMLSelectElement;
    const input = screen.getByRole("textbox", { name: "Unsubmitted project name" }) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "My draft" } });
    expect(menu.disabled).toBe(true);
    expect(account.disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Chinese" }));
    expect(mockCallApi).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByTestId("locale-state").textContent).toBe("auto/en-US/false/none");

    mockAuthState = { user: { id: 7 }, isLoading: false };
    mockCallApi.mockResolvedValueOnce(response(null, "en-US", "fallback"));
    rerenderApp();
    await waitFor(() => expect(menu.disabled).toBe(false));
    await waitFor(() => expect(mockCallApi).toHaveBeenCalledTimes(1));
    expect(account.disabled).toBe(false);
    mockCallApi.mockResolvedValueOnce(response("zh-CN", "zh-CN"));
    fireEvent.change(menu, { target: { value: "zh-CN" } });
    await waitFor(() => expect(runtime.locale).toBe("zh-CN"));
    expect(mockCallApi).toHaveBeenCalledWith("updateCurrentUserLocale", expect.objectContaining({
      body: { preference: "zh-CN" }, headers: { "X-CSRFToken": "test-csrf" },
    }));
    expect(menu.value).toBe("zh-CN");
    expect(account.value).toBe("zh-CN");
    expect(input.value).toBe("My draft");
    expect(screen.queryByRole("alert")).toBeNull();
    unmount();
    runtime.destroy();
  });

  it("keeps confirmed actor language controls usable during background account refresh", async () => {
    mockCallApi.mockResolvedValueOnce(response(null, "en-US", "fallback"));
    const { runtime, unmount, rerenderApp } = mount();
    await waitFor(() => expect(mockCallApi).toHaveBeenCalledTimes(1));
    mockAuthState = { user: { id: 7 }, isLoading: true };
    rerenderApp();
    const menu = screen.getByTestId("menu-language-select") as HTMLSelectElement;
    const account = screen.getByTestId("language-preference-select") as HTMLSelectElement;
    expect(menu.disabled).toBe(false);
    expect(account.disabled).toBe(false);
    mockCallApi.mockResolvedValueOnce(response("zh-CN", "zh-CN"));
    fireEvent.change(account, { target: { value: "zh-CN" } });
    await waitFor(() => expect(runtime.locale).toBe("zh-CN"));
    expect(mockCallApi).toHaveBeenCalledWith("updateCurrentUserLocale", expect.objectContaining({
      body: { preference: "zh-CN" }, headers: { "X-CSRFToken": "test-csrf" },
    }));
    expect(screen.queryByRole("alert")).toBeNull();
    unmount();
    runtime.destroy();
  });
});
