import React from "react";

type State = { output: string };

export default class Admin extends React.Component<unknown, State> {
  state: State = { output: "" };

  run(code: string) {
    // Dynamic code evaluation must be reported, not analyzed.
    this.setState({ output: String(eval(code)) });
  }

  render() {
    return <pre>{this.state.output}</pre>;
  }
}
