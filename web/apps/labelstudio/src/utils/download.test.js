import { downloadFile } from "./download";

it("downloads the original blob and releases its temporary URL after the click", () => {
  jest.useFakeTimers();
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  URL.createObjectURL = jest.fn(() => "blob:release-copy");
  URL.revokeObjectURL = jest.fn();
  const click = jest.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function () {
    expect(this.download).toBe("submission-21-revision-3.json");
    expect(this.href).toBe("blob:release-copy");
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
  });
  try {
    const blob = new Blob(["immutable server bytes"]);
    downloadFile(blob, "submission-21-revision-3.json");
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob);
    expect(click).toHaveBeenCalledTimes(1);
    jest.runOnlyPendingTimers();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:release-copy");
  } finally {
    click.mockRestore();
    URL.createObjectURL = originalCreate;
    URL.revokeObjectURL = originalRevoke;
    jest.useRealTimers();
  }
});
