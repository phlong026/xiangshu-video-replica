import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// The full suite runs many jsdom workers at once. Give async effects enough time
// to settle under CI load while preserving the default polling behavior.
configure({ asyncUtilTimeout: 3000 });

afterEach(() => {
  cleanup();
});
