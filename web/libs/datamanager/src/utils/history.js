import { isDefined } from "./utils";

export const History = {
  getParams(urlInstance) {
    const url = urlInstance ?? new URL(window.location.href);
    const result = {};

    url.searchParams.forEach((value, key) => {
      result[key] = value;
    });

    return result;
  },

  setParams(params = {}) {
    const url = new URL(window.location.href);
    const { searchParams } = url;

    Object.entries(params).forEach(([key, value]) => {
      if (value === null || value === undefined) {
        searchParams.delete(key);
      } else {
        searchParams.set(key, value);
      }
    });

    return url;
  },

  navigate(params = {}, replace = false) {
    const url = this.setParams(params);
    const title = document.title;
    const state = this.getParams(url);

    if (replace) {
      window.history.replaceState(state, title, url.toString());
    } else {
      window.history.pushState(state, title, url.toString());
    }
  },

  forceNavigate(params = {}, replace = false) {
    const resultParams = { ...params };
    const currentParams = this.getParams();

    // Editor transitions replace task/view parameters, not the fixed package
    // work context. An explicit null still ends that context.
    if (!("task_package" in resultParams) && "task_package" in currentParams) {
      resultParams.task_package = currentParams.task_package;
    }
    if (!("package_review" in resultParams) && "package_review" in currentParams) {
      resultParams.package_review = currentParams.package_review;
    }

    Object.entries(currentParams).forEach(([key]) => {
      if (!isDefined(resultParams[key])) {
        resultParams[key] = null;
      }
    });

    if (currentParams.query) {
      resultParams.query = currentParams.query;
    }

    this.navigate(resultParams, replace);
  },
};
