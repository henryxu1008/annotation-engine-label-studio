import { Button, buttonVariant, ToastContext, ToastType } from "@humansignal/ui";
import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { generatePath, useHistory } from "react-router";
import { Link, NavLink } from "react-router-dom";
import { Spinner } from "../../components";
import { modal } from "../../components/Modal/Modal";
import { Space } from "../../components/Space/Space";
import { useAPI } from "../../providers/ApiProvider";
import { useAuth } from "@humansignal/core/providers/AuthProvider";
import { useProject } from "../../providers/ProjectProvider";
import { useContextProps, useParams } from "../../providers/RoutesProvider";
import { addCrumb, deleteCrumb } from "../../services/breadrumbs";
import { cn } from "../../utils/bem";
import { isDefined } from "../../utils/helpers";
import { ImportModal } from "../CreateProject/Import/ImportModal";
import { ExportPage } from "../ExportPage/ExportPage";
import { APIConfig } from "./api-config";
import { AssignmentManager } from "./AssignmentManager";
import { ReviewerWorkspace } from "./ReviewerWorkspace";
import { SubmissionReleaseWorkspace } from "./SubmissionReleaseWorkspace";
import { isDisplayLocale, useLocalePreference, useLocaleTranslation } from "@humansignal/i18n";

import "./DataManager.scss";

const loadDependencies = () => [import("@humansignal/datamanager"), import("@humansignal/editor")];

const initializeDataManager = (root, props, params) => {
  if (!window.LabelStudio) throw Error("Label Studio Frontend doesn't exist on the page");
  if (!root || root.dataset.dmInitialized) return;

  root.dataset.dmInitialized = true;

  const { ...settings } = root.dataset;

  const dmConfig = {
    root,
    projectId: params.id,
    apiGateway: `${window.APP_SETTINGS.hostname}/api/dm`,
    apiVersion: 2,
    project: params.project,
    polling: window.APP_SETTINGS?.polling,
    showPreviews: false,
    apiEndpoints: APIConfig.endpoints,
    interfaces: {
      import: true,
      export: true,
      backButton: false,
      labelingHeader: false,
      autoAnnotation: params.autoAnnotation,
    },
    labelStudio: {
      keymap: window.APP_SETTINGS.editor_keymap,
    },
    ...props,
    ...settings,
    locale: params.locale,
  };

  return new window.DataManager(dmConfig);
};

const buildLink = (path, params) => {
  return generatePath(`/projects/:id${path}`, params);
};

const CollaborationModalTitle = ({ kind, taskId }) => {
  const { t } = useLocaleTranslation("collaboration");
  if (kind === "assignments") return t("taskAssignmentsTitle", { id: taskId });
  if (kind === "reviews") return t("reviewSubmissionsTitle");
  return t("releaseWorkspaceTitle");
};

const CollaborationModalLanguage = ({ initialPreference, setPreference }) => {
  const { t } = useLocaleTranslation("app");
  const { user } = useAuth();
  const ready = Boolean(user?.id);
  const [preference, setLocalPreference] = useState(initialPreference ?? "auto");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(false);

  const change = async (event) => {
    if (!ready) return;
    const next = event.target.value;
    if (next !== "auto" && !isDisplayLocale(next)) return;
    setSaving(true);
    setError(false);
    const saved = await setPreference(next === "auto" ? null : next);
    if (saved) setLocalPreference(next);
    else setError(true);
    setSaving(false);
  };

  return <div className="flex items-center gap-2">
    <label htmlFor="collaboration-language-select">{t("displayLanguage")}</label>
    <select
      id="collaboration-language-select"
      data-testid="collaboration-language-select"
      aria-label={t("displayLanguage")}
      value={preference}
      disabled={!ready || saving}
      onChange={change}
    >
      <option value="auto">{t("languageAutomatic")}</option>
      <option value="en-US">English</option>
      <option value="zh-CN">简体中文</option>
    </select>
    {error && <span role="alert">{t("languageSaveFailed")}</span>}
  </div>;
};

export const DataManagerPage = ({ ...props }) => {
  const { locale, t } = useLocaleTranslation("datamanager");
  const latestLocale = useRef(locale);
  latestLocale.current = locale;
  const dependencies = useMemo(loadDependencies, []);
  const toast = useContext(ToastContext);
  const root = useRef();
  const params = useParams();
  const history = useHistory();
  const api = useAPI();
  const { project } = useProject();
  const setContextProps = useContextProps();
  const [crashed, setCrashed] = useState(false);
  const [loading, setLoading] = useState(!window.DataManager || !window.LabelStudio);
  const dataManagerRef = useRef();
  const projectId = project?.id;

  const init = useCallback(async (isCancelled) => {
    if (!window.LabelStudio) return;
    if (!window.DataManager) return;
    if (!root.current) return;
    if (!project?.id) return;
    if (dataManagerRef.current) return;

    const mlBackends = await api.callApi("mlBackends", {
      params: { project: project.id },
    });

    // A slow backend response can arrive after navigation or a newer init
    // effect. Never construct a workspace for the disposed page.
    if (isCancelled() || !root.current || dataManagerRef.current) return;

    const interactiveBacked = (mlBackends ?? []).find(({ is_interactive }) => is_interactive);

    const dataManager = initializeDataManager(root.current, props, {
      ...params,
      project,
      autoAnnotation: isDefined(interactiveBacked),
      locale: latestLocale.current,
    });
    if (!dataManager) return;
    dataManagerRef.current = dataManager;

    Object.assign(window, { dataManager });

    dataManager.on("crash", (details) => {
      if (details?.phase === "initialization") {
        setCrashed(true);
        return;
      }
      const error = details?.error;
      const isMissingTaskError = error?.startsWith("Task ID:");
      const isMissingProjectError = error?.startsWith("Project ID:");

      if (isMissingTaskError || isMissingProjectError) {
        const message = t(isMissingTaskError ? "missingTask" : "missingProject");

        toast.show({
          message,
          type: ToastType.error,
          duration: 10000,
        });
      }

      if (isMissingTaskError) {
        history.push(buildLink("", { id: params?.id ?? project?.id }));
      } else if (isMissingProjectError) {
        history.push("/projects");
      }
    });

    dataManager.on("settingsClicked", () => {
      history.push(buildLink("/settings/labeling", { id: params?.id ?? project?.id }));
    });

    dataManager.on("importClicked", () => {
      history.push(buildLink("/data/import", { id: params?.id ?? project?.id }));
    });

    // Navigate to Storage Settings and auto-open Add Source Storage modal
    dataManager.on("openSourceStorageModal", () => {
      history.push(buildLink("/settings/storage?open=source", { id: params?.id ?? project?.id }));
    });

    dataManager.on("exportClicked", () => {
      history.push(buildLink("/data/export", { id: params?.id ?? project?.id }));
    });

    dataManager.on("error", (response) => {
      api.handleError(response);
    });

    dataManager.on("toast", ({ message, type, id, duration }) => {
      toast.show({ message, type, id, duration });
    });

    dataManager.on("toast:dismiss", ({ id } = {}) => {
      toast.dismiss(id);
    });

    dataManager.on("navigate", (route) => {
      const target = route.replace(/^projects/, "");

      if (target) history.push(buildLink(target, { id: params?.id ?? project?.id }));
      else history.push("/projects");
    });

    if (interactiveBacked) {
      dataManager.on("lsf:regionFinishedDrawing", (reg, group) => {
        const { lsf, task, currentAnnotation: annotation } = dataManager.lsf;
        const ids = group.map((r) => r.cleanId);
        const result = annotation.serializeAnnotation().filter((res) => ids.includes(res.id));

        const suggestionsRequest = api.callApi("mlInteractive", {
          params: { pk: interactiveBacked.id },
          body: {
            task: task.id,
            context: { result },
          },
        });

        // we'll check that we are processing the same task
        const wrappedRequest = new Promise(async (resolve, reject) => {
          const response = await suggestionsRequest;

          // right now task might be an old task,
          // so in order to get a current one we need to get it from lsf
          if (task.id === dataManager.lsf.task.id) {
            resolve(response);
          } else {
            reject();
          }
        });

        lsf.loadSuggestions(wrappedRequest, (response) => {
          if (response.data) {
            return response.data.result;
          }

          return null;
        });
      });
    }

    setContextProps({ dmRef: dataManager });
  }, [projectId]);

  const destroyDM = useCallback(() => {
    if (dataManagerRef.current) {
      dataManagerRef.current.destroy();
      dataManagerRef.current = null;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all(dependencies)
      .then(() => {
        if (cancelled) return;
        setLoading(false);
        return init(() => cancelled);
      });
    return () => { cancelled = true; };
  }, [init]);

  useEffect(() => {
    // destroy the data manager when the component is unmounted
    return () => destroyDM();
  }, []);

  useEffect(() => {
    dataManagerRef.current?.setLocale(locale);
  }, [locale]);

  return crashed ? (
    <div className={cn("crash").toClassName()}>
      <div className={cn("crash").elem("info").toClassName()}>{t("workspaceLoadFailed")}</div>

      <Link to="/projects" className={buttonVariant()} aria-label={t("backToProjects")}>
        {t("backToProjects")}
      </Link>
    </div>
  ) : (
    <>
      {loading && (
        <div className="flex-1 absolute inset-0 flex items-center justify-center">
          <Spinner size={64} />
        </div>
      )}
      {/* Allow this to exist before the DataManager is initialized as the async app.fetchData call eventually calls startLabeling, and that requires the root element to exist */}
      <div ref={root} className={cn("datamanager").toClassName()} />
    </>
  );
};

DataManagerPage.path = "/data";
DataManagerPage.pages = {
  ExportPage,
  ImportModal,
};
DataManagerPage.context = ({ dmRef }) => {
  const { locale, t } = useLocaleTranslation("datamanager");
  const { t: tc } = useLocaleTranslation("collaboration");
  const localePreference = useLocalePreference();
  const { project } = useProject();
  const api = useAPI();
  const toast = useContext(ToastContext);
  const [mode, setMode] = useState(dmRef?.mode ?? "explorer");
  const [canManageAssignments, setCanManageAssignments] = useState(false);
  const [canReviewSubmissions, setCanReviewSubmissions] = useState(false);

  useEffect(() => {
    let active = true;

    const probe = async () => {
      if (!project?.id) {
        setCanManageAssignments(false);
        return;
      }

      const result = await api.callApi("projectMemberCapability", {
        params: { pk: project.id },
        errorFilter: (apiError) => [403, 404].includes(apiError?.status),
      });

      if (!active) return;
      const status = result?.status ?? result?.$meta?.status;
      setCanManageAssignments(Boolean(result?.can_manage && !result?.error && status !== 403 && status !== 404));
    };

    probe();
    return () => {
      active = false;
    };
  }, [project?.id]);

  useEffect(() => {
    let active = true;

    const probe = async () => {
      if (!project?.id) {
        setCanReviewSubmissions(false);
        return;
      }

      const result = await api.callApi("projectReviewCapability", {
        params: { pk: project.id },
        errorFilter: (apiError) => [403, 404].includes(apiError?.status),
      });

      if (!active) return;
      const status = result?.status ?? result?.$meta?.status;
      setCanReviewSubmissions(Boolean(result?.can_review && !result?.error && status !== 403 && status !== 404));
    };

    probe();
    return () => {
      active = false;
    };
  }, [project?.id]);

  const openAssignments = () => {
    if (!localePreference.ready) return;
    const taskId = dmRef?.store?.taskStore?.selected?.id;

    if (!taskId) {
      toast.show({
        message: tc("openTaskBeforeAssignments"),
        type: ToastType.error,
      });
      return;
    }

    modal({
      title: <CollaborationModalTitle kind="assignments" taskId={taskId} />,
      header: <CollaborationModalLanguage initialPreference={localePreference.preference} setPreference={localePreference.setPreference} />,
      body: <AssignmentManager projectId={project.id} taskId={taskId} />,
      style: { width: 640 },
    });
  };

  const openReviews = () => {
    if (!localePreference.ready) return;
    modal({
      title: <CollaborationModalTitle kind="reviews" />,
      header: <CollaborationModalLanguage initialPreference={localePreference.preference} setPreference={localePreference.setPreference} />,
      body: <ReviewerWorkspace projectId={project.id} />,
      style: { width: 960 },
    });
  };

  const openReleases = () => {
    if (!localePreference.ready) return;
    modal({
      title: <CollaborationModalTitle kind="releases" />,
      header: <CollaborationModalLanguage initialPreference={localePreference.preference} setPreference={localePreference.setPreference} />,
      body: <SubmissionReleaseWorkspace projectId={project.id} />,
      style: { width: 960 },
    });
  };

  const links = {
    "/settings": t("settings"),
  };

  const updateCrumbs = (currentMode) => {
    const isExplorer = currentMode === "explorer";
    deleteCrumb("dm-crumb");
    if (!isExplorer) {
      addCrumb({
        key: "dm-crumb",
        title: t("labeling"),
      });
    }
  };

  const showLabelingInstruction = (currentMode) => {
    const isLabelStream = currentMode === "labelstream";
    const { expert_instruction, show_instruction } = project;

    if (isLabelStream && show_instruction && expert_instruction) {
      modal({
        title: t("labelingInstructions"),
        body: <div dangerouslySetInnerHTML={{ __html: expert_instruction }} />,
        style: { width: 680 },
      });
    }
  };

  const onDMModeChanged = (currentMode) => {
    setMode(currentMode);
    showLabelingInstruction(currentMode);
  };

  useEffect(() => {
    if (dmRef) {
      dmRef.on("modeChanged", onDMModeChanged);
    }

    return () => {
      dmRef?.off?.("modeChanged", onDMModeChanged);
    };
  }, [dmRef, project, locale]);

  useEffect(() => {
    updateCrumbs(mode);
  }, [mode, locale]);

  return project && project.id ? (
    <Space size="small">
      {canManageAssignments && (
        <Button
          size="small"
          look="outlined"
          disabled={mode === "explorer" || !localePreference.ready}
          onClick={openAssignments}
          data-testid="manage-task-assignments"
        >
          {tc("assignments")}
        </Button>
      )}

      {canManageAssignments && (
        <Button
          size="small"
          look="outlined"
          onClick={openReleases}
          disabled={!localePreference.ready}
          data-testid="open-release-workspace"
        >
          {tc("releases")}
        </Button>
      )}

      {canReviewSubmissions && (
        <Button
          size="small"
          look="outlined"
          onClick={openReviews}
          disabled={!localePreference.ready}
          data-testid="open-review-workspace"
        >
          {tc("reviews")}
        </Button>
      )}

      {project.expert_instruction && mode !== "explorer" && (
        <Button
          size="small"
          look="outlined"
          onClick={() => {
            modal({
              title: t("instructions"),
              body: () => (
                <div
                  dangerouslySetInnerHTML={{
                    __html: project.expert_instruction,
                  }}
                />
              ),
            });
          }}
        >
          {t("instructions")}
        </Button>
      )}

      {Object.entries(links).map(([path, label]) => (
        <Link
          key={path}
          tag={NavLink}
          className={buttonVariant({ size: "small", look: "outlined" })}
          to={`/projects/${project.id}${path}`}
          data-external
        >
          {label}
        </Link>
      ))}
    </Space>
  ) : null;
};
