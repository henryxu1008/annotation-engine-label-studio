import { useCallback, useEffect, useMemo, useRef, useState, type PropsWithChildren } from "react";
import { useAuth } from "@humansignal/core/providers/AuthProvider";
import {
  DEFAULT_LOCALE,
  isDisplayLocale,
  LocalePreferenceContext,
  useLocaleRuntime,
  type LocalePreference,
  type LocalePreferenceState,
} from "@humansignal/i18n";
import { useAPI } from "./ApiProvider";

const SOURCES = new Set(["user", "cookie", "accept-language", "deployment", "fallback"]);

function validState(value: any): value is LocalePreferenceState {
  return value && isDisplayLocale(value.resolvedLocale)
    && (value.preference === null || isDisplayLocale(value.preference))
    && SOURCES.has(value.source);
}

function initialState(): LocalePreferenceState {
  const initial = window.APP_SETTINGS?.locale;
  return validState({ ...initial, preference: initial?.userPreference })
    ? { preference: initial.userPreference, resolvedLocale: initial.resolvedLocale, source: initial.source }
    : { preference: null, resolvedLocale: DEFAULT_LOCALE, source: "fallback" };
}

function csrfToken(): string | null {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || null;
}

export const LocalePreferenceProvider = ({ children }: PropsWithChildren) => {
  const runtime = useLocaleRuntime();
  const { user, isLoading } = useAuth();
  // AuthProvider exposes a user only after the actor query succeeds. Its
  // loading flag also covers background refreshes of that confirmed actor.
  const ready = Boolean(user?.id);
  const { callApi } = useAPI();
  const stableCallApi = useRef(callApi).current;
  const [state, setState] = useState(initialState);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const userId = useRef(window.APP_SETTINGS?.user?.id);
  const requestId = useRef(0);

  const apply = useCallback((next: LocalePreferenceState) => {
    setState(next);
    runtime.updateLocale(next.resolvedLocale);
    document.documentElement.lang = next.resolvedLocale;
    document.documentElement.dataset.localeSource = next.source;
  }, [runtime]);

  useEffect(() => {
    if (user?.id || isLoading) return;
    ++requestId.current;
    userId.current = undefined;
    apply({ preference: null, resolvedLocale: DEFAULT_LOCALE, source: "fallback" });
    setSaving(false);
    setError(null);
  }, [user?.id, isLoading, apply]);

  useEffect(() => {
    if (!user?.id) return;
    const currentRequest = ++requestId.current;
    if (userId.current !== user.id) {
      // A new account may not inherit the last account's explicit language.
      userId.current = user.id;
      apply({ preference: null, resolvedLocale: DEFAULT_LOCALE, source: "fallback" });
      setSaving(false);
      setError(null);
    }
    void stableCallApi("currentUserLocale", { suppressError: true }).then((result) => {
      if (currentRequest !== requestId.current || userId.current !== user.id) return;
      if (result?.$meta?.ok && validState(result)) apply(result);
    }).catch(() => undefined);
    return () => { requestId.current++; };
  }, [user?.id, stableCallApi, apply]);

  const setPreference = useCallback(async (preference: LocalePreference) => {
    if (preference !== null && !isDisplayLocale(preference)) return false;
    // SSR menu visibility does not confirm the session's authenticated actor.
    if (!ready || !user?.id || userId.current !== user.id) return false;
    const token = csrfToken();
    if (!token) {
      setError("saveFailed");
      return false;
    }
    const currentRequest = ++requestId.current;
    const currentUser = user.id;
    setSaving(true);
    setError(null);
    try {
      const result = await stableCallApi("updateCurrentUserLocale", {
        body: { preference },
        headers: { "X-CSRFToken": token },
        suppressError: true,
      });
      if (currentRequest !== requestId.current || userId.current !== currentUser) return false;
      if (result?.$meta?.ok && validState(result) && result.preference === preference) {
        apply(result);
        return true;
      }
      setError("saveFailed");
      return false;
    } catch {
      if (currentRequest === requestId.current) setError("saveFailed");
      return false;
    } finally {
      if (currentRequest === requestId.current) setSaving(false);
    }
  }, [ready, user?.id, stableCallApi, apply]);

  const value = useMemo(() => ({ ...state, ready, saving, error, setPreference }), [state, ready, saving, error, setPreference]);
  return <LocalePreferenceContext value={value}>{children}</LocalePreferenceContext>;
};
