import { isDisplayLocale, useLocalePreference, useLocaleTranslation } from "@humansignal/i18n";

export const LanguagePreferences = ({ compact = false }: { compact?: boolean }) => {
  const { t } = useLocaleTranslation("app");
  const { preference, ready, saving, error, setPreference } = useLocalePreference();
  const inputId = compact ? "menu-display-language" : "account-display-language";
  const descriptionId = `${inputId}-description`;

  return (
    <div className="flex flex-col gap-2" data-testid={compact ? "menu-language-preference" : "account-language-preference"}>
      <label htmlFor={inputId}>{t("displayLanguage")}</label>
      {!compact && <p id={descriptionId}>{t("languageDescription")}</p>}
      <select
        id={inputId}
        aria-describedby={compact ? undefined : descriptionId}
        value={preference ?? "auto"}
        onChange={(event) => {
          const selected = event.target.value;
          if (selected === "auto" || isDisplayLocale(selected)) void setPreference(selected === "auto" ? null : selected);
        }}
        disabled={!ready || saving}
        data-testid={compact ? "menu-language-select" : "language-preference-select"}
        className="h-9 w-fit rounded-md border border-neutral-border bg-primary text-primary-content px-3"
      >
        <option value="auto">{t("languageAutomatic")}</option>
        <option value="en-US">English</option>
        <option value="zh-CN">简体中文</option>
      </select>
      {error && <p role="alert" className="text-negative-content">{t("languageSaveFailed")}</p>}
    </div>
  );
};
