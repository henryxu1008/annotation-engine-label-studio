import { createContext, useContext, type PropsWithChildren } from "react";
import type { DisplayLocale } from "./runtime";

export type LocalePreference = DisplayLocale | null;
export type LocalePreferenceState = {
  preference: LocalePreference;
  resolvedLocale: DisplayLocale;
  source: "user" | "cookie" | "accept-language" | "deployment" | "fallback";
};

export type LocalePreferenceContextValue = LocalePreferenceState & {
  ready: boolean;
  saving: boolean;
  error: string | null;
  setPreference: (preference: LocalePreference) => Promise<boolean>;
};

const PreferenceContext = createContext<LocalePreferenceContextValue | null>(null);

export const LocalePreferenceContext = ({ value, children }: PropsWithChildren<{ value: LocalePreferenceContextValue }>) => (
  <PreferenceContext.Provider value={value}>{children}</PreferenceContext.Provider>
);

export function useLocalePreference(): LocalePreferenceContextValue {
  const value = useContext(PreferenceContext);
  if (!value) throw new Error("Locale preference provider is required");
  return value;
}
