import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import "@testing-library/jest-dom/vitest";

// Testing Library only auto-registers cleanup when Vitest globals are enabled.
// They are not — tests import what they use — so without this every render
// accumulates in the document and queries start finding several of everything.
afterEach(cleanup);
