import enCommon from "./catalogs/en-US/common.json";
import enApp from "./catalogs/en-US/app.json";
import enProjects from "./catalogs/en-US/projects.json";
import enDatamanager from "./catalogs/en-US/datamanager.json";
import enEditor from "./catalogs/en-US/editor.json";
import enCollaboration from "./catalogs/en-US/collaboration.json";
import enErrors from "./catalogs/en-US/errors.json";
import enTaskPackages from "./catalogs/en-US/taskPackages.json";
import enReviewBatches from "./catalogs/en-US/reviewBatches.json";
import zhCommon from "./catalogs/zh-CN/common.json";
import zhApp from "./catalogs/zh-CN/app.json";
import zhProjects from "./catalogs/zh-CN/projects.json";
import zhDatamanager from "./catalogs/zh-CN/datamanager.json";
import zhEditor from "./catalogs/zh-CN/editor.json";
import zhCollaboration from "./catalogs/zh-CN/collaboration.json";
import zhErrors from "./catalogs/zh-CN/errors.json";
import zhTaskPackages from "./catalogs/zh-CN/taskPackages.json";
import zhReviewBatches from "./catalogs/zh-CN/reviewBatches.json";

export const namespaces = [
  "common",
  "app",
  "projects",
  "datamanager",
  "editor",
  "collaboration",
  "errors",
  "taskPackages",
  "reviewBatches",
] as const;
export type LocaleNamespace = (typeof namespaces)[number];
export const resources = {
  "en-US": {
    common: enCommon,
    app: enApp,
    projects: enProjects,
    datamanager: enDatamanager,
    editor: enEditor,
    collaboration: enCollaboration,
    errors: enErrors,
    taskPackages: enTaskPackages,
    reviewBatches: enReviewBatches,
  },
  "zh-CN": {
    common: zhCommon,
    app: zhApp,
    projects: zhProjects,
    datamanager: zhDatamanager,
    editor: zhEditor,
    collaboration: zhCollaboration,
    errors: zhErrors,
    taskPackages: zhTaskPackages,
    reviewBatches: zhReviewBatches,
  },
};
