import { Component } from "react";
import { EditorLocaleRoot } from "./EditorLocaleRoot";
import { configureStore } from "./configureStore";
import { createLocaleRuntime } from "@humansignal/i18n";
import { destroy } from "mobx-state-tree";

export class LabelStudio extends Component {
  localeRuntime = createLocaleRuntime(this.props.locale);
  unmounted = false;
  state = {
    initialized: false,
  };

  componentDidMount() {
    const initialTask = this.props.task;
    configureStore(this.props, undefined, this.localeRuntime).then(({ store }) => {
      if (this.unmounted) {
        destroy(store);
        return;
      }
      this.store = store;
      if (this.props.task !== initialTask) {
        store.resetState();
        store.assignTask(this.props.task);
        store.initializeStore(this.props.task);
      }
      window.Htx = this.store;
      this.setState({ initialized: true });
    });
  }

  componentDidUpdate(prevProps) {
    if (this.props.locale !== prevProps.locale) this.localeRuntime.updateLocale(this.props.locale);
    if (this.store && this.props.task !== prevProps.task) {
      this.store.resetState();
      this.store.assignTask(this.props.task);
      this.store.initializeStore(this.props.task);
    }
  }

  componentWillUnmount() {
    this.unmounted = true;
    this.localeRuntime.destroy();
    if (this.store) {
      destroy(this.store);
      if (window.Htx === this.store) window.Htx = null;
      this.store = null;
    }
  }

  render() {
    return this.state.initialized ? <EditorLocaleRoot runtime={this.localeRuntime} store={this.store} /> : null;
  }
}
