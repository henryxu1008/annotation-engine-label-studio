import ReactDOM from "react-dom";
import { App } from "../components/App/App";
import { AppStore } from "../stores/AppStore";
import * as DataStores from "../stores/DataStores";
import { DynamicModel, registerModel } from "../stores/DynamicModel";
import { types } from "mobx-state-tree";
import { ConfigProvider as AntdConfigProvider } from "antd";
import { getAntdLocale, useLocaleTranslation } from "@humansignal/i18n";

const DMLocaleAdapter = ({ children }) => {
  const { locale } = useLocaleTranslation("common");
  return <AntdConfigProvider locale={getAntdLocale(locale)}>{children}</AntdConfigProvider>;
};

const createDynamicModels = (columns) => {
  const grouppedColumns = columns.reduce((res, column) => {
    res.set(column.target, res.get(column.target) ?? []);
    res.get(column.target).push(column);
    return res;
  }, new Map());

  grouppedColumns.forEach((columns, target) => {
    const dataStore = DataStores[target].create?.(columns);

    if (dataStore) registerModel(`${target}Store`, dataStore);
  });

  if (columns.length === 0) {
    registerModel("tasksStore", DataStores.tasks?.create());
  }

  /** temporary solution until we'll have annotations */
  registerModel("annotationsStore", DataStores.annotations?.create());
};

/**
 * Create DM React app
 * @param {HTMLElement} rootNode
 * @param {import("./dm-sdk").DataManager} datamanager
 * @returns {Promise<AppStore|null>}
 */
export const createApp = async (rootNode, datamanager, isCurrent = () => true) => {
  const isLabelStream = datamanager.mode === "labelstream";
  const restricted = datamanager.settings?.blindProtocol === true;
  const projectId = datamanager.projectId;

  const response = restricted
    ? await datamanager.api.blindColumns({ project: projectId })
    : await datamanager.api.columns();
  // The API request can outlive a destroyed or reloaded Data Manager instance.
  if (!isCurrent()) return null;
  // Never apply columns fetched for a previous restricted project or mode.
  if ((datamanager.settings?.blindProtocol === true) !== restricted
      || (restricted && String(datamanager.projectId) !== String(projectId))) return null;

  if (response?.error) {
    datamanager.invoke("error", response);
    datamanager.invoke("crash", { ...response, phase: "initialization" });
    return null;
  }

  if (!response) {
    const message = `
      ${response?.error ?? ""}
      LS API not available; check \`API_GATEWAY\` and \`LS_ACCESS_TOKEN\` env vars;
      also check \`data-project-id\` in \`public/index.html\`
    `;

    throw new Error(message);
  }

  const columns = response.columns ?? response;
  if (!Array.isArray(columns)) throw new TypeError("Invalid Data Manager columns response");

  createDynamicModels(columns);

  // types.late is resolved once in MST, although,
  // we need it to be resolved each time DM is initialized
  // so both taskStore and annotationStore moved here to dynamically
  // initialized
  //
  // without it the columns set will be cached during navigation resulting
  // in incorrect dynamic columns and mapping mismatch
  const FinalStore = types.compose(
    AppStore,
    types.model("DataStore", {
      taskStore: types.optional(
        types.late(() => DynamicModel.get("tasksStore")),
        {},
      ),
      annotationStore: types.optional(
        types.late(() => DynamicModel.get("annotationsStore")),
        {},
      ),
    }),
  );

  const appStore = FinalStore.create({
    viewsStore: {
      views: [],
      columnsRaw: columns,
    },
    project: datamanager.project ?? {},
    mode: datamanager.mode,
    showPreviews: datamanager.showPreviews,
    interfaces: Object.fromEntries(datamanager.interfaces),
    toolbar: datamanager.toolbar,
    availableActions: Array.from(datamanager.actions.values()).map(({ action }) => action),
  });

  appStore._sdk = datamanager;

  appStore.fetchData({ isLabelStream });

  window.DM = appStore;

  const LocaleProvider = datamanager.localeRuntime.provider;
  ReactDOM.render(<LocaleProvider><DMLocaleAdapter><App app={appStore} /></DMLocaleAdapter></LocaleProvider>, rootNode);

  return appStore;
};
