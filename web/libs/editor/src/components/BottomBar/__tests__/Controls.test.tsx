import { act, fireEvent } from "@testing-library/react";
import { renderWithLocale as render } from "../../../__tests__/localeTestUtils";
import { Provider } from "mobx-react";
import { Controls } from "../Controls";

jest.mock("@humansignal/ui", () => {
  const { forwardRef } = jest.requireActual("react");
  return {
    ButtonGroup: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    Button: forwardRef(({ children, ...props }: { children: React.ReactNode }) => {
      return (
        <button data-testid="button" {...props}>
          {children}
        </button>
      );
    }),
    Tooltip: ({ children }: { children: React.ReactNode }) => {
      return <div data-testid="tooltip">{children}</div>;
    },
    Userpic: ({ children }: { children: React.ReactNode }) => {
      return (
        <div
          data-testid="userpic"
          className="userpic--tBKCQ"
          style={{ background: "rgb(155, 166, 211)", color: "rgb(0, 0, 0)" }}
        >
          {children}
        </div>
      );
    },
  };
});
const mockStore = {
  hasInterface: jest.fn(),
  isSubmitting: false,
  settings: {
    enableTooltips: true,
  },
  task: { id: 1, allow_skip: true },
  skipTask: jest.fn(),
  commentStore: {
    currentComment: {
      a3r0fa: "It's working",
      a0lsuf: "It's working fine",
    },
    commentFormSubmit: jest.fn(),
    setTooltipMessage: jest.fn(),
  },
  annotationStore: {
    selected: {
      submissionInProgress: jest.fn(),
      history: {
        canUndo: false,
      },
    },
  },
  customButtons: new Map(),
};

const mockHistory = {
  canUndo: false,
};

const mockAnnotation = {
  id: "a31wsd",
  canBeReviewed: false,
  userGenerate: false,
  sentUserGenerate: false,
  versions: {},
  results: [],
  editable: true,
};

// Helper to set up window.APP_SETTINGS for enterprise and role-based tests
const setupAppSettings = (options: { role?: string; enterprise?: boolean } = {}) => {
  (window as any).APP_SETTINGS = {
    user: {
      role: options.role,
    },
    billing: {
      enterprise: options.enterprise ?? false,
    },
  };
};

describe("Controls", () => {
  test.each([true, false])("keeps autosave active while comments are pending before submit/update: new=%s", async (userGenerate) => {
    let complete!: () => void;
    const selected = { ...mockAnnotation, userGenerate, submissionInProgress: jest.fn() };
    const submitAnnotation = jest.fn();
    const updateAnnotation = jest.fn();
    const store = { ...mockStore, submitAnnotation, updateAnnotation,
      hasInterface: (name: string) => ["submit", "update"].includes(name),
      annotationStore: { selected },
      commentStore: { ...mockStore.commentStore, commentFormSubmit: () => new Promise<void>((resolve) => { complete = resolve; }) },
    };
    const { getByTestId } = render(<Provider store={store}><Controls history={mockHistory} annotation={selected} /></Provider>);
    fireEvent.click(getByTestId(userGenerate ? "bottombar-submit-button" : "bottombar-update-button"));
    expect(selected.submissionInProgress).not.toHaveBeenCalled();
    expect(submitAnnotation).not.toHaveBeenCalled();
    expect(updateAnnotation).not.toHaveBeenCalled();
    await act(async () => complete());
    expect(userGenerate ? submitAnnotation : updateAnnotation).toHaveBeenCalledTimes(1);
  });
  test("keeps review acceptance separate from readonly result editing", () => {
    const readonlyReview = { ...mockAnnotation, history: mockHistory, canBeReviewed: true, isReadOnly: () => true };
    const reviewStore = { ...mockStore, hasInterface: (name: string) => name === "review",
      annotationStore: { ...mockStore.annotationStore, selected: readonlyReview } };
    const { getByTestId } = render(<Provider store={reviewStore}>
      <Controls history={mockHistory} annotation={readonlyReview} />
    </Provider>);
    expect(getByTestId("bottombar-accept-button")).not.toBeDisabled();
  });

  test.each([true, false])("disables readonly annotation writes even when empty results are allowed: new=%s", (userGenerate) => {
    const readonlyStore = {
      ...mockStore,
      hasInterface: (name: string) => ["submit", "update", "skip"].includes(name),
      submitAnnotation: jest.fn(),
      updateAnnotation: jest.fn(),
    };
    const readonlyAnnotation = { ...mockAnnotation, userGenerate, isReadOnly: () => true };
    const { getByTestId } = render(<Provider store={readonlyStore}>
      <Controls history={mockHistory} annotation={readonlyAnnotation} />
    </Provider>);
    const save = getByTestId(userGenerate ? "bottombar-submit-button" : "bottombar-update-button");
    expect(save).toBeDisabled();
    expect(getByTestId("bottombar-skip-button")).toBeDisabled();
    fireEvent.click(save);
    expect(readonlyStore.submitAnnotation).not.toHaveBeenCalled();
    expect(readonlyStore.updateAnnotation).not.toHaveBeenCalled();
  });

  beforeEach(() => {
    jest.clearAllMocks();
    // Reset APP_SETTINGS before each test
    (window as any).APP_SETTINGS = undefined;
    // Reset mockStore task to default
    mockStore.task = { id: 1, allow_skip: true };
  });

  test("When skip button is clicked, if there is no currentComment and annotators must leave a comment on skip, it must not submit and setToolTipMessage", () => {
    mockStore.hasInterface = (a: string) => (a === "skip" || a === "comments:skip") ?? true;

    const { getByTestId } = render(
      <Provider store={mockStore}>
        <Controls history={mockHistory} annotation={mockAnnotation} />
      </Provider>,
    );

    const skipTask = getByTestId("bottombar-skip-button");
    fireEvent.click(skipTask);

    expect(mockStore.skipTask).not.toHaveBeenCalled();
    expect(mockStore.commentStore.commentFormSubmit).not.toHaveBeenCalled();
    expect(mockStore.commentStore.setTooltipMessage).toHaveBeenCalledWith("Please enter a comment before skipping");
  });

  test("When skip button is clicked, but there is an empty message on currentComment and annotators must leave a comment on skip, it must not submit and setToolTipMessage", () => {
    mockStore.hasInterface = (a: string) => (a === "skip" || a === "comments:skip") ?? true;
    mockStore.commentStore.currentComment.a31wsd = "   ";

    const { getByTestId } = render(
      <Provider store={mockStore}>
        <Controls history={mockHistory} annotation={mockAnnotation} />
      </Provider>,
    );

    const skipTask = getByTestId("bottombar-skip-button");
    fireEvent.click(skipTask);

    expect(mockStore.skipTask).not.toHaveBeenCalled();
    expect(mockStore.commentStore.commentFormSubmit).not.toHaveBeenCalled();
    expect(mockStore.commentStore.setTooltipMessage).toHaveBeenCalledWith("Please enter a comment before skipping");
  });

  test("When skip button is clicked, if there is no currentComment and annotators doesn't need to leave a comment on skip, it must submit", async () => {
    mockStore.hasInterface = (a: string) => a === "skip";

    const { getByTestId } = render(
      <Provider store={mockStore}>
        <Controls history={mockHistory} annotation={mockAnnotation} />
      </Provider>,
    );

    const skipTask = getByTestId("bottombar-skip-button");
    fireEvent.click(skipTask);

    await expect(mockStore.commentStore.commentFormSubmit).toHaveBeenCalled();
    expect(mockStore.skipTask).toHaveBeenCalled();
  });

  test("Skip button NOT disabled when allow_skip=false in LSO (non-enterprise)", () => {
    // In LSO (non-enterprise), allow_skip field doesn't exist/affect behavior
    setupAppSettings({ enterprise: false });
    mockStore.hasInterface = (a: string) => a === "skip";
    mockStore.task = { id: 1, allow_skip: false };

    const { getByTestId } = render(
      <Provider store={mockStore}>
        <Controls history={mockHistory} annotation={mockAnnotation} />
      </Provider>,
    );

    const skipTask = getByTestId("bottombar-skip-button");
    // In LSO, skip button should NOT be disabled even when allow_skip=false
    expect(skipTask).not.toBeDisabled();
  });

  test("Skip button disabled when allow_skip=false in LSE (enterprise)", () => {
    setupAppSettings({ enterprise: true });
    mockStore.hasInterface = (a: string) => a === "skip";
    mockStore.task = { id: 1, allow_skip: false };

    const { getByTestId } = render(
      <Provider store={mockStore}>
        <Controls history={mockHistory} annotation={mockAnnotation} />
      </Provider>,
    );

    const skipTask = getByTestId("bottombar-skip-button");
    expect(skipTask).toBeDisabled();
  });

  test("Skip button enabled when allow_skip=true in LSE (enterprise)", () => {
    setupAppSettings({ enterprise: true });
    mockStore.hasInterface = (a: string) => a === "skip";
    mockStore.task = { id: 1, allow_skip: true };

    const { getByTestId } = render(
      <Provider store={mockStore}>
        <Controls history={mockHistory} annotation={mockAnnotation} />
      </Provider>,
    );

    const skipTask = getByTestId("bottombar-skip-button");
    expect(skipTask).not.toBeDisabled();
  });

  test("Skip action blocked when allow_skip=false in LSE (enterprise)", () => {
    setupAppSettings({ enterprise: true });
    mockStore.hasInterface = (a: string) => a === "skip";
    mockStore.task = { id: 1, allow_skip: false };
    mockStore.skipTask.mockClear();

    const { getByTestId } = render(
      <Provider store={mockStore}>
        <Controls history={mockHistory} annotation={mockAnnotation} />
      </Provider>,
    );

    const skipTask = getByTestId("bottombar-skip-button");
    fireEvent.click(skipTask);

    expect(mockStore.skipTask).not.toHaveBeenCalled();
  });
});
