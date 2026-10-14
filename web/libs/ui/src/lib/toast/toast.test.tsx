import { act, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { useContext } from "react";
import { ToastContext, ToastProvider, ToastViewport } from "./toast";

jest.mock("./toast.module.scss", () => ({}));
jest.mock("../../assets/icons", () => ({ IconCross: () => <span /> }));
jest.mock("@humansignal/shad/utils", () => ({ cn: (...args: string[]) => args.join(" ") }));
jest.mock("nanoid", () => ({ nanoid: () => "test-toast" }));

function Probe({ duration }: { duration?: number }) {
  const toast = useContext(ToastContext)!;
  return <>
    <button onClick={() => toast.show({ id: "assignment", message: "Task is read-only", duration })}>Show</button>
    <button onClick={() => toast.dismiss("other")}>Dismiss other</button>
    <button onClick={() => toast.dismiss("assignment")}>Dismiss assignment</button>
  </>;
}

beforeEach(() => { jest.useFakeTimers(); });
afterEach(() => { jest.useRealTimers(); });

it("keeps the documented -1 notification open until its own ID is dismissed", () => {
  render(<ToastProvider><Probe duration={-1} /><ToastViewport /></ToastProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Show", exact: true }));
  act(() => { jest.advanceTimersByTime(20000); });
  expect(screen.getByText("Task is read-only")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss other" }));
  expect(screen.getByText("Task is read-only")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss assignment" }));
  expect(screen.queryByText("Task is read-only")).not.toBeInTheDocument();
});

it("still closes a normal notification after its duration", () => {
  render(<ToastProvider><Probe duration={4000} /><ToastViewport /></ToastProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Show", exact: true }));
  expect(screen.getByText("Task is read-only")).toBeVisible();
  act(() => { jest.advanceTimersByTime(5000); });
  expect(screen.queryByText("Task is read-only")).not.toBeInTheDocument();
});
