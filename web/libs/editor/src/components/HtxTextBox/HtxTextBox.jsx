import React from "react";
import { IconPencil, IconTrashAlt, IconCheck } from "@humansignal/icons";
import { Button, Tooltip, Typography } from "@humansignal/ui";
import { throttle } from "@humansignal/core/lib/utils/lodash-replacements";
import { cn } from "../../utils/bem";
import { isComposingKeyEvent } from "../../utils/events";
import { isAlive, isStateTreeNode } from "mobx-state-tree";
import { guidGenerator } from "../../core/Helpers";
import { useLocaleTranslation } from "@humansignal/i18n";

// used for correct auto-height calculation
const BORDER_WIDTH = 1;

class HtxTextBoxView extends React.Component {
  pendingInputKey = guidGenerator();
  state = {
    editing: false,
    height: 0,
    value: this.props.text,
  };

  inputClassName =
    "text-color-neutral-content bg-neutral-surface border border-neutral-border px-base py-tight rounded-md w-full focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary-focus-outline leading-body-small min-h-10 hover:border-neutral-border-bold transition-colors transition-background-color duration-150 ease-out";

  textRef = React.createRef();
  inputRef = React.createRef();

  static getDerivedStateFromProps(props, state) {
    if (props.text !== state.prevPropsText) {
      return {
        value: props.text,
        prevPropsText: props.text,
      };
    }
    return null;
  }

  componentDidMount() {
    window.addEventListener("click", this.handleGlobalClick, { capture: true });
    this.syncPendingInput();
  }

  componentDidUpdate(previousProps) {
    if (previousProps.annotation !== this.props.annotation) this.clearPendingInput(previousProps.annotation);
    this.syncPendingInput();
  }

  componentWillUnmount() {
    window.removeEventListener("click", this.handleGlobalClick, { capture: true });
    this.clearPendingInput(this.props.annotation);
  }

  clearPendingInput = (annotation) => {
    if (isStateTreeNode(annotation) && !isAlive(annotation)) return;
    annotation?.setPendingTextInput?.(this.pendingInputKey, false);
  };

  syncPendingInput = () => {
    const annotation = this.props.annotation;
    if (isStateTreeNode(annotation) && !isAlive(annotation)) return;
    annotation?.setPendingTextInput?.(this.pendingInputKey,
      (this.state.editing || !!this.props.onlyEdit) && this.state.value !== this.props.text);
  };

  handleGlobalClick = (e) => {
    const el = e?.target;
    const isShortcut = el?.dataset?.shortcut;
    const shouldSkip =
      !this.state.editing || (this.props.ignoreShortcuts && isShortcut) || el === this.inputRef.current;

    if (!shouldSkip) {
      this.setEditing(false);
    }
  };

  startEditing = () => {
    const height = this.textRef.current?.parentNode.offsetHeight || 0;

    this.setState({ editing: true, height });

    // eslint-disable-next-line no-unused-expressions
    this.props.onStartEditing?.();

    setTimeout(this.focus);
  };

  focus = () => {
    const input = this.inputRef.current;

    if (input) input.selectionStart = this.state.value.length;
  };

  setEditing = (editing) => {
    this.setState({ editing });
  };

  setValue = (value) => {
    this.setState({ value });
  };

  cancel = () => {
    this.setValue(this.props.text);
    this.setEditing(false);
  };

  save = () => {
    this.props.onChange(this.state.value);
    this.setEditing(false);
  };

  updateHeight = throttle(() => {
    // very important to add borders to the height, otherwise input will be shrinking on every recalc
    const scrollHeight = this.inputRef.current?.scrollHeight ?? 0;
    const height = scrollHeight + BORDER_WIDTH * 2;

    // initially scrollHeight can be 0, so we won't change height
    if (scrollHeight && height !== this.state.height) {
      this.setState({ height });
    }
  }, 100);

  renderEdit() {
    const {
      className = "",
      rows = 1,
      onlyEdit,
      name,
      onFocus,
      onChange,

      // don't pass non-DOM props to Paragraph
      onDelete: _,
      isEditable: __,
      isDeleteable: ___,
      ignoreShortcuts: ____,
      annotation: _____,
      t,
    } = this.props;
    const { height, value } = this.state;

    const inputProps = {
      name,
      className: `flex ${this.inputClassName}`,
      style: height ? { height, borderWidth: BORDER_WIDTH } : null,
      autoFocus: true,
      ref: this.inputRef,
      value,
      "data-testid": "htx-textbox-input",
      onBlur: () => {
        onChange(this.state.value);
      },
      onFocus,
      onChange: (e) => {
        this.setValue(e.target.value);
        this.updateHeight();
      },
      onKeyDown: (e) => {
        if (isComposingKeyEvent(e)) {
          if (e.key === "Enter") e.preventDefault();
          return;
        }
        const { key, shiftKey } = e;

        if (key === "Enter") {
          // for multiline textarea save only by shift+enter
          if (+rows === 1 || shiftKey) {
            e.preventDefault();
            e.stopPropagation();
            this.save();
          }
        } else if (key === "Escape") {
          this.cancel();
        } else if (key === "Tab") {
          this.setEditing(false);
        }
      },
    };

    this.updateHeight();

    return (
      <div className={cn("textarea").elem("region").toClassName()} data-testid="htx-textbox-edit">
        {rows > 1 ? <textarea {...inputProps} /> : <input {...inputProps} />}
        {!onlyEdit && (
          <Tooltip title={t("textConfirmHint", { shortcut: rows > 1 ? "Shift+Enter" : "Enter" })}>
            <Button
              type="text"
              variant="primary"
              look="outlined"
              size="small"
              className="absolute right-tight top-tighter"
              icon={<IconCheck />}
              aria-label={t("textConfirmChanges")}
              data-testid="htx-textbox-save"
              onClick={this.save}
            />
          </Tooltip>
        )}
      </div>
    );
  }

  renderView() {
    const {
      onChange,
      onDelete,
      isEditable,
      isDeleteable,
      text,

      // don't pass non-DOM props to Paragraph
      ignoreShortcuts: _,
      onlyEdit: __,
      annotation: ___,
      t,

      ...props
    } = this.props;

    return (
      <div className={cn("textarea").elem("region").toClassName()} data-testid="htx-textbox-view">
        <div className={this.inputClassName} id={props.id} name={props.name} data-testid="htx-textbox-content">
          <Typography ref={this.textRef} size="small">
            {text.split("\n").map((line, index, array) => {
              const isLastLine = index === array.length - 1;
              return (
                <React.Fragment key={index}>
                  {line}
                  {!isLastLine && <br />}
                </React.Fragment>
              );
            })}
          </Typography>
        </div>
        <div className={cn("textarea").elem("actions").toClassName()} data-testid="htx-textbox-actions">
          {isEditable && onChange && (
            <Button
              variant="neutral"
              look="outlined"
              size="small"
              tooltip={t("textEdit")}
              tooltipTheme="Dark"
              leading={<IconPencil />}
              aria-label={t("textEdit")}
              data-testid="htx-textbox-edit-button"
              onClick={this.startEditing}
            />
          )}
          {isDeleteable && onDelete && (
            <Button
              type="text"
              variant="negative"
              look="outlined"
              size="small"
              tooltip={t("textDelete")}
              tooltipTheme="Dark"
              leading={<IconTrashAlt />}
              aria-label={t("textDelete")}
              data-testid="htx-textbox-delete-button"
              onClick={onDelete}
            />
          )}
        </div>
      </div>
    );
  }

  render() {
    return (this.state.editing || this.props.onlyEdit) && this.props.isEditable ? this.renderEdit() : this.renderView();
  }
}

export const HtxTextBox = React.forwardRef((props, ref) => {
  const { t } = useLocaleTranslation("editor");
  return <HtxTextBoxView {...props} t={t} ref={ref} />;
});
HtxTextBox.displayName = "HtxTextBox";
