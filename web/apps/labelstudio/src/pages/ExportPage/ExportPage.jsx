import { useEffect, useRef, useState, useCallback } from "react";
import { useHistory } from "react-router";
import { Button, Badge } from "@humansignal/ui";
import {
  IconWarningCircleFilled,
  IconTerminal,
  IconCode,
  IconBook,
  IconExternal,
  IconCopyOutline,
} from "@humansignal/icons";
import { Form, Input } from "../../components/Form";
import { Modal } from "../../components/Modal/Modal";
import { Space } from "../../components/Space/Space";
import { useAPI } from "../../providers/ApiProvider";
import { useFixedLocation, useParams } from "../../providers/RoutesProvider";
import { cn } from "../../utils/bem";
import { isDefined, copyText } from "../../utils/helpers";
import { formatDisplayNumber, useLocaleRuntime, useLocaleTranslation } from "@humansignal/i18n";
import { displayExportFormat } from "./exportFormatDisplay";
import "./ExportPage.scss";

// Community Edition exports run synchronously in a single HTTP request.
// Large exports can exceed typical proxy timeouts, so we warn early and link to alternatives.
const LARGE_EXPORT_TASK_THRESHOLD = 1000;
const EXPORT_TIMEOUT_DOCS_URL = "https://labelstud.io/guide/export.html#Export-timeout-in-Community-Edition";
const EXPORT_CONSOLE_DOCS_URL = "https://labelstud.io/guide/export.html#Export-using-console-command";
const EXPORT_SNAPSHOT_SDK_URL = "https://api.labelstud.io/api-reference/api-reference/projects/exports/create";
const ENTERPRISE_URL = "https://docs.humansignal.com/guide/label_studio_compare";

// const formats = {
//   json: 'JSON',
//   csv: 'CSV',
// };

const downloadFile = (blob, filename) => {
  const link = document.createElement("a");

  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.click();
};

const wait = () => new Promise((resolve) => setTimeout(resolve, 5000));

const isTimeoutLikeStatus = (status) => status === 408 || status === 502 || status === 504;

export const ExportPage = () => {
  const { t } = useLocaleTranslation("datamanager");
  const history = useHistory();
  const location = useFixedLocation();
  const pageParams = useParams();
  const api = useAPI();

  const [downloading, setDownloading] = useState(false);
  const [downloadingMessage, setDownloadingMessage] = useState(false);
  const [availableFormats, setAvailableFormats] = useState([]);
  const [currentFormat, setCurrentFormat] = useState("JSON");
  const [projectTaskNumber, setProjectTaskNumber] = useState(null);
  const [exportIssue, setExportIssue] = useState(null);
  const [formatIssue, setFormatIssue] = useState("loading");

  /** @type {import('react').RefObject<Form>} */
  const form = useRef();

  const proceedExport = async () => {
    if (formatIssue || !availableFormats.some((format) => format.name === currentFormat && !format.disabled)) return;
    setExportIssue(null);
    setDownloading(true);

    const messageTimer = window.setTimeout(() => {
      setDownloadingMessage(true);
    }, 1000);

    try {
      const params = form.current.assembleFormData({
        asJSON: true,
        full: true,
        booleansAsNumbers: true,
      });

      const response = await api.callApi("exportRaw", {
        params: {
          pk: pageParams.id,
          ...params,
        },
      });

      // The API proxy can return `null` for certain network errors; treat it as timeout-like
      // and show actionable guidance instead of a generic error.
      if (!response) {
        setExportIssue("timeout");
        return;
      }

      if (response.ok) {
        const blob = await response.blob();

        downloadFile(blob, response.headers.get("filename"));
        return;
      }

      if (isTimeoutLikeStatus(response.status)) {
        setExportIssue("timeout");
        return;
      }

      const error = await response.clone().json().catch(() => null);
      if (error?.display_context?.reason === "IMMUTABLE_RELEASE_REQUIRED") {
        setFormatIssue("immutable");
        setAvailableFormats([]);
        return;
      }

      api.handleError(response);
    } finally {
      window.clearTimeout(messageTimer);
      setDownloading(false);
      setDownloadingMessage(false);
    }
  };

  useEffect(() => {
    if (isDefined(pageParams.id)) {
      let cancelled = false;
      setFormatIssue("loading");
      setAvailableFormats([]);

      api
        .callApi("exportFormats", {
          params: {
            pk: pageParams.id,
          },
          errorFilter: () => true,
        })
        .then((formats) => {
          if (cancelled) return;
          const available = Array.isArray(formats) ? formats : [];
          setFormatIssue(formats?.response?.display_context?.reason === "IMMUTABLE_RELEASE_REQUIRED"
            ? "immutable" : available.some((format) => !format.disabled) ? null : "unavailable");
          setAvailableFormats(available);
          setCurrentFormat(available.find((format) => !format.disabled)?.name ?? "JSON");
        });

      // Fetch project metadata to show a proactive warning for large exports.
      // This is best-effort and should not trigger global error UI if it fails.
      api
        .callApi("project", {
          params: { pk: pageParams.id },
          errorFilter: () => true,
        })
        .then((project) => {
          if (cancelled) return;
          setProjectTaskNumber(project?.task_number ?? null);
        });

      return () => {
        cancelled = true;
      };
    }
  }, [pageParams.id]);

  return (
    <Modal
      onHide={() => {
        const path = location.pathname.replace(ExportPage.path, "");
        const search = location.search;

        history.replace(`${path}${search !== "?" ? search : ""}`);
      }}
      title={t("exportData")}
      style={{ width: 720 }}
      closeOnClickOutside={false}
      allowClose={!downloading}
      // footer="Read more about supported export formats in the Documentation."
      visible
    >
      <div className={cn("export-page").toClassName()}>
        {!formatIssue && <FormatInfo
          availableFormats={availableFormats}
          selected={currentFormat}
          onClick={(format) => setCurrentFormat(format.name)}
        />}

        {formatIssue && (
          <div className={cn("export-page").elem("warning").toClassName()} role="status">
            {formatIssue === "immutable" ? <>
              <strong>{t("exportImmutableTitle")}</strong>
              <p>{t("exportImmutableDescription")}</p>
            </> : formatIssue === "loading" ? t("exportFormatsLoading") : t("exportFormatsUnavailable")}
          </div>
        )}

        {!formatIssue && <ExportLargeProjectWarning taskCount={projectTaskNumber} />}
        {exportIssue === "timeout" && <ExportTimeoutGuidance projectId={pageParams.id} exportType={currentFormat} />}

        <Form ref={form}>
          <Input type="hidden" name="exportType" value={currentFormat} />
        </Form>

        <div className={cn("export-page").elem("footer").toClassName()}>
          {downloadingMessage && (
            <div className={cn("export-page").elem("status-message").toClassName()}>
              {t("exportPreparing")}
            </div>
          )}
          <Space style={{ width: "100%" }} spread>
            <div className={cn("export-page").elem("recent").toClassName()}>
              <a className="no-go" href={EXPORT_TIMEOUT_DOCS_URL} target="_blank" rel="noreferrer">
                {t("exportTimeoutHelp")}
              </a>
            </div>
            <div className={cn("export-page").elem("actions").toClassName()}>
              <Button className="w-[135px]" onClick={proceedExport} waiting={downloading} disabled={Boolean(formatIssue)} aria-label={t("exportData")}>
                {t("export")}
              </Button>
            </div>
          </Space>
        </div>
      </div>
    </Modal>
  );
};

const FormatInfo = ({ availableFormats, selected, onClick }) => {
  const { t } = useLocaleTranslation("datamanager");
  const runtime = useLocaleRuntime();
  return (
    <div className={cn("formats").toClassName()}>
      <div className={cn("formats").elem("info").toClassName()}>
        {t("exportFormatsIntro")}
      </div>
      <div className={cn("formats").elem("list").toClassName()}>
        {availableFormats.map((format) => {
          const display = displayExportFormat(format, runtime);
          const selectFormat = () => {
            if (!format.disabled) onClick(format);
          };
          return (
            <div
              key={format.name}
              className={cn("formats")
                .elem("item")
                .mod({
                  active: !format.disabled,
                  selected: format.name === selected,
                })
                .toClassName()}
              role="button"
              tabIndex={format.disabled ? -1 : 0}
              aria-pressed={format.name === selected}
              aria-disabled={Boolean(format.disabled)}
              aria-label={display.title}
              data-testid={`export-format-${format.name}`}
              onClick={selectFormat}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  selectFormat();
                }
              }}
            >
              <div className={cn("formats").elem("name").toClassName()}>
                {display.title}

                <Space size="small">
                  {format.tags?.map?.((tag, index) => {
                    // Keep badge variants tied to the converter's raw tag, never its translation.
                    const tagLower = tag?.toLowerCase() || "";
                    let variant = "primary";
                    if (tagLower === "enterprise" || tagLower.includes("enterprise")) {
                      variant = "gradient";
                    } else if (tagLower === "beta") {
                      variant = "plum";
                    } else if (tagLower === "new" || tagLower.includes("new")) {
                      variant = "positive";
                    }

                    return (
                      <Badge key={index} variant={variant} size="small">
                        {display.tags[index]}
                      </Badge>
                    );
                  })}
                </Space>
              </div>

              {display.description && (
                <div className={cn("formats").elem("description").toClassName()}>{display.description}</div>
              )}
            </div>
          );
        })}
      </div>
      <div className={cn("formats").elem("feedback").toClassName()}>
        {t("exportMissingFormat")}
        <br />
        {t("exportMissingFormatContact")}{" "}
        <a className="no-go" href="https://slack.labelstud.io/?source=product-export" target="_blank" rel="noreferrer">
          {t("exportSlack")}
        </a>{" "}
        {t("exportMissingFormatOrIssue")}{" "}
        <a
          className="no-go"
          href="https://github.com/HumanSignal/label-studio-converter/issues"
          target="_blank"
          rel="noreferrer"
        >
          {t("exportRepository")}
        </a>
      </div>
    </div>
  );
};

ExportPage.path = "/export";
ExportPage.modal = true;

const ExportLargeProjectWarning = ({ taskCount }) => {
  const { locale, t } = useLocaleTranslation("datamanager");
  if (!Number.isFinite(taskCount) || taskCount < LARGE_EXPORT_TASK_THRESHOLD) return null;

  return (
    <div className={cn("export-page").elem("warning").toClassName()}>
      <div className={cn("export-page").elem("warning-title").toClassName()}>
        {t("exportLargeProject", { count: formatDisplayNumber(taskCount, locale) })}
      </div>
      <div className={cn("export-page").elem("warning-body").toClassName()}>
        {t("exportLargeIntro")}{" "}
        <a className="no-go" href={EXPORT_TIMEOUT_DOCS_URL} target="_blank" rel="noreferrer">
          {t("exportCliSdkOptions")}
        </a>{" "}
        {t("exportLargeOrConsider")}{" "}
        <a className="no-go" href={ENTERPRISE_URL} target="_blank" rel="noreferrer">
          {t("exportEnterprise")}
        </a>{" "}
        {t("exportLargeOutro")}
      </div>
    </div>
  );
};

const ExportTimeoutGuidance = ({ projectId, exportType }) => {
  const { t } = useLocaleTranslation("datamanager");
  const cliCommand = `label-studio export ${projectId} ${exportType} --export-path=<output-path>`;
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(() => {
    copyText(cliCommand);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [cliCommand]);

  return (
    <div className={cn("export-page").elem("timeout").toClassName()}>
      <div className={cn("export-page").elem("timeout-header").toClassName()}>
        <IconWarningCircleFilled className={cn("export-page").elem("timeout-icon").toClassName()} />
        <div className={cn("export-page").elem("timeout-title").toClassName()}>{t("exportTimedOut")}</div>
      </div>
      <div className={cn("export-page").elem("timeout-body").toClassName()}>
        {t("exportTimeoutExplanation")}
      </div>

      <div className={cn("export-page").elem("timeout-actions").toClassName()}>
        <div className={cn("export-page").elem("timeout-actions-title").toClassName()}>{t("exportRecommendedOptions")}</div>
        <ul className={cn("export-page").elem("timeout-actions-list").toClassName()}>
          <li>
            <div className={cn("export-page").elem("timeout-action-item").toClassName()}>
              <IconTerminal className={cn("export-page").elem("timeout-action-icon").toClassName()} />
              <div className={cn("export-page").elem("timeout-action-content").toClassName()}>
                <span>
                  {t("exportUsing")}{" "}
                  <a className="no-go" href={EXPORT_CONSOLE_DOCS_URL} target="_blank" rel="noreferrer">
                    {t("exportConsoleCommand")}
                    <IconExternal className={cn("export-page").elem("timeout-link-icon").toClassName()} />
                  </a>
                  :
                </span>
                <div className={cn("export-page").elem("timeout-code-wrapper").toClassName()}>
                  <pre className={cn("export-page").elem("timeout-code").toClassName()}>
                    <code>{cliCommand}</code>
                  </pre>
                  <button
                    type="button"
                    className={cn("export-page").elem("timeout-copy-button").toClassName()}
                    onClick={handleCopy}
                    aria-label={t("exportCopyCommand")}
                    title={copied ? t("exportCopied") : t("exportCopyCommand")}
                  >
                    <IconCopyOutline className={cn("export-page").elem("timeout-copy-icon").toClassName()} />
                    {copied && (
                      <span className={cn("export-page").elem("timeout-copy-text").toClassName()}>{t("exportCopied")}</span>
                    )}
                  </button>
                </div>
              </div>
            </div>
          </li>
          <li>
            <div className={cn("export-page").elem("timeout-action-item").toClassName()}>
              <IconCode className={cn("export-page").elem("timeout-action-icon").toClassName()} />
              <div className={cn("export-page").elem("timeout-action-content").toClassName()}>
                {t("exportUse")}{" "}
                <a className="no-go" href={EXPORT_SNAPSHOT_SDK_URL} target="_blank" rel="noreferrer">
                  {t("exportSdkSnapshots")}
                  <IconExternal className={cn("export-page").elem("timeout-link-icon").toClassName()} />
                </a>{" "}
                {t("exportSdkOutro")}
              </div>
            </div>
          </li>
          <li>
            <div className={cn("export-page").elem("timeout-action-item").toClassName()}>
              <IconWarningCircleFilled className={cn("export-page").elem("timeout-action-icon").toClassName()} />
              <div className={cn("export-page").elem("timeout-action-content").toClassName()}>
                {t("exportLargeScaleIntro")}{" "}
                <a className="no-go" href={ENTERPRISE_URL} target="_blank" rel="noreferrer">
                  {t("exportEnterpriseFull")}
                  <IconExternal className={cn("export-page").elem("timeout-link-icon").toClassName()} />
                </a>{" "}
                {t("exportLargeScaleOutro")}
              </div>
            </div>
          </li>
        </ul>
        <div className={cn("export-page").elem("timeout-footer").toClassName()}>
          <IconBook className={cn("export-page").elem("timeout-footer-icon").toClassName()} />
          <span>
            {t("exportMoreDetails")}{" "}
            <a className="no-go" href={EXPORT_TIMEOUT_DOCS_URL} target="_blank" rel="noreferrer">
              {t("exportTimeoutDocs")}
              <IconExternal className={cn("export-page").elem("timeout-link-icon").toClassName()} />
            </a>
          </span>
        </div>
      </div>
    </div>
  );
};
