import { render } from "@testing-library/react";

import App from "./App";

// Test files are skipped; this component must not be extracted.
export function TestHarness() {
  return <App />;
}

render(<TestHarness />);
