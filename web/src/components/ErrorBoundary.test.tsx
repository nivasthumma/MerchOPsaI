// The crash screen. A component that throws used to surface React Router's own
// error page — stack frames and a message addressed to the developer — which is
// what a viewer of this project actually saw once.

import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { AppErrorBoundary } from "./ErrorBoundary";

function Boom(): JSX.Element {
  throw new Error("verification_detail is not a string");
}

function renderCrash() {
  const router = createMemoryRouter(
    [{ path: "/", element: <Boom />, errorElement: <AppErrorBoundary /> }],
    { initialEntries: ["/"] },
  );
  return render(<RouterProvider router={router} />);
}

describe("app error boundary", () => {
  it("frames the failure instead of showing the router's default page", () => {
    // React logs the caught error; that noise is not the subject of the test.
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    renderCrash();
    expect(screen.getByText("This page failed to render")).toBeInTheDocument();
    spy.mockRestore();
  });

  it("still shows what went wrong, rather than swallowing it", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    renderCrash();
    // A control plane that hides its own failures teaches people to distrust
    // everything else it reports.
    // Twice on purpose: the message, and again inside the stack disclosure.
    expect(screen.getAllByText(/verification_detail is not a string/).length)
      .toBeGreaterThan(0);
    expect(screen.getByText("stack")).toBeInTheDocument();
    spy.mockRestore();
  });

  it("says plainly that no financial state was touched", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    renderCrash();
    expect(screen.getByText(/no task, approval or financial state changed/))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
    spy.mockRestore();
  });
});
