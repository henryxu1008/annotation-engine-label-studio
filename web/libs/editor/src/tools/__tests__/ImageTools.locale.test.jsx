import { act, fireEvent, render, screen } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { Zoom } from "../Zoom";
import { Selection } from "../Selection";

jest.mock("../../components/Toolbar/Tool", () => ({
  Tool: ({ label, onClick, active }) => <button aria-pressed={active} onClick={onClick}>{label}</button>,
}));
jest.mock("../../components/Toolbar/FlyoutMenu", () => ({
  FlyoutMenu: ({ items }) => <div>{items.map(({ label, onClick }) => <button key={label} onClick={onClick}>{label}</button>)}</div>,
}));

it("switches high-frequency image tools live without losing selection or their actions", () => {
  const manager = { selectTool: jest.fn() };
  const object = { handleZoom: jest.fn(), sizeToFit: jest.fn(), sizeToOriginal: jest.fn() };
  const zoom = Zoom.create({ selected: true }, { manager, object });
  const selection = Selection.create({ selected: true }, { manager, object });
  const ZoomView = zoom.viewClass;
  const SelectionView = selection.viewClass;
  const runtime = createLocaleRuntime("en-US");
  const Provider = runtime.provider;
  const view = render(<Provider><ZoomView /><SelectionView /></Provider>);
  expect(screen.getByRole("button", { name: "Pan Image" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("button", { name: "Move" }).getAttribute("aria-pressed")).toBe("true");
  act(() => { runtime.updateLocale("zh-CN"); });
  expect(screen.getByRole("button", { name: "平移图片" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "移动区域" }));
  expect(manager.selectTool).toHaveBeenCalledWith(selection, false);
  fireEvent.click(screen.getByRole("button", { name: "放大" }));
  fireEvent.click(screen.getByRole("button", { name: "缩小" }));
  fireEvent.click(screen.getByRole("button", { name: "适应窗口" }));
  fireEvent.click(screen.getByRole("button", { name: "原始尺寸" }));
  expect(object.handleZoom.mock.calls).toEqual([[1], [-1]]);
  expect(object.sizeToFit).toHaveBeenCalledTimes(1);
  expect(object.sizeToOriginal).toHaveBeenCalledTimes(1);
  act(() => { runtime.updateLocale("en-US"); });
  expect(screen.getByRole("button", { name: "Zoom to fit" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Zoom to actual size" })).toBeTruthy();
  view.unmount();
  runtime.destroy();
});
