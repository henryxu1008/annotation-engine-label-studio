import { History } from "./history";

afterEach(() => jest.restoreAllMocks());

it("retains package context across task transitions and permits explicit project return", () => {
  jest.spyOn(History, "getParams").mockReturnValue({ task: "8", task_package: "4", package_review: "1" });
  const navigate = jest.spyOn(History, "navigate").mockImplementation(() => {});
  const next = { task: 9 };
  History.forceNavigate(next);
  expect(navigate).toHaveBeenLastCalledWith({ task: 9, task_package: "4", package_review: "1" }, false);
  expect(next).toEqual({ task: 9 });
  History.forceNavigate({ task_package: null, package_review: null });
  expect(navigate).toHaveBeenLastCalledWith({ task: null, task_package: null, package_review: null }, false);
});
