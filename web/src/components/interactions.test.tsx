// The interactive layer added in the visual pass. Two of these encode
// judgements rather than mechanics: a success toast may disappear on its own,
// a refusal may not, and the stepper must show a halted task as halted rather
// than as progressing.

import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";
import { DensityToggle } from "./Chrome";
import { CommandPalette, internalRoute } from "./CommandPalette";
import { Stepper } from "./Stepper";
import { ThemeToggle } from "./Theme";
import { ToastHost, useToast } from "./Toast";
import type { Task } from "../api/types";
import taskFixture from "../test-fixtures/task.json";

const TASK = taskFixture as unknown as Task;


describe("theme control", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
  });

  it("defaults to the system theme rather than forcing one", () => {
    render(<ThemeToggle />);
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("cycles system → light → dark → system and remembers the choice", async () => {
    render(<ThemeToggle />);
    const btn = screen.getByRole("button");

    await userEvent.click(btn);
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(localStorage.getItem("merchantops.theme")).toBe("light");

    await userEvent.click(btn);
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");

    await userEvent.click(btn);
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(localStorage.getItem("merchantops.theme")).toBeNull();
  });
});

function Trigger({ tone }: { tone: "ok" | "danger" }) {
  const toast = useToast();
  return (
    <button onClick={() => toast({ tone, title: tone === "ok" ? "Approved" : "Refused",
                                   body: "detail" })}>
      fire
    </button>
  );
}

describe("toasts", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  it("lets a success message dismiss itself", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<ToastHost><Trigger tone="ok" /></ToastHost>);
    await user.click(screen.getByRole("button", { name: "fire" }));
    expect(screen.getByText("Approved")).toBeInTheDocument();

    await act(async () => { vi.advanceTimersByTime(5000); });
    expect(screen.queryByText("Approved")).toBeNull();
  });

  it("keeps a failure on screen until it is dismissed", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<ToastHost><Trigger tone="danger" /></ToastHost>);
    await user.click(screen.getByRole("button", { name: "fire" }));

    // A refusal that vanishes on its own is how someone concludes the action
    // went through.
    await act(async () => { vi.advanceTimersByTime(30000); });
    expect(screen.getByText("Refused")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Refused")).toBeNull();
  });
});

describe("lifecycle stepper", () => {
  it("marks a halted task as blocked at approval, not as progressing", () => {
    const halted: Task = { ...TASK, status: "AWAITING_APPROVAL", actions: [] };
    render(<Stepper task={halted} />);
    const approval = screen.getByText(/Approval/);
    expect(approval).toHaveClass("blocked");
    expect(screen.getByText(/Verify/)).not.toHaveClass("done");
  });

  it("marks execution and verification done once they have happened", () => {
    render(<Stepper task={TASK} />);
    expect(screen.getByText(/Execute/)).toHaveClass("done");
    expect(screen.getByText(/Verify/)).toHaveClass("done");
  });
});

describe("command palette", () => {
  beforeEach(() => { localStorage.clear(); document.documentElement.removeAttribute("data-theme"); });

  function open() {
    render(<MemoryRouter><CommandPalette /></MemoryRouter>);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
  }

  it("opens on ⌘K and closes on Escape", async () => {
    open();
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();
    // `userEvent.keyboard` dispatches on the focused element and lets the
    // event bubble, which is how a person actually presses Escape.
    // `fireEvent.keyDown(window, …)` targets `window` directly, so it reaches
    // a listener on `window` and no listener on `document` — a distinction no
    // real keystroke makes, and one that made this test pass or fail on which
    // of the two the implementation happened to pick.
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps the promise `aria-modal` makes", async () => {
    // Declaring the page behind it inert and then letting Tab walk into that
    // page is the defect this pins. Same contract as the action drawer, from
    // the same hook.
    open();
    const dialog = screen.getByRole("dialog", { name: "Command palette" });

    // Typing is the whole reason to open this, so the input takes focus.
    expect(document.activeElement).toBe(
      screen.getByRole("textbox", { name: "Command" }));

    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select, textarea,'
      + ' [tabindex]:not([tabindex="-1"])'));
    focusable[focusable.length - 1].focus();
    await userEvent.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("filters and runs a command with the keyboard", async () => {
    open();
    const input = screen.getByLabelText("Command");
    await userEvent.type(input, "theme");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    await userEvent.keyboard("{Enter}");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("offers no way to approve or execute anything", async () => {
    // Approving a refund two keystrokes after typing three letters is exactly
    // the frictionless action this system exists to prevent.
    open();
    const labels = screen.getAllByRole("option").map((o) => o.textContent?.toLowerCase() ?? "");
    for (const forbidden of ["approve", "refund", "execute", "reject"]) {
      expect(labels.some((l) => l.includes(forbidden))).toBe(false);
    }
  });
});

describe("density", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-density");
  });

  it("defaults to comfortable and remembers compact", async () => {
    render(<DensityToggle />);
    expect(document.documentElement.getAttribute("data-density")).toBe("comfortable");

    await userEvent.click(screen.getByRole("button"));
    expect(document.documentElement.getAttribute("data-density")).toBe("compact");
    expect(localStorage.getItem("merchantops.density")).toBe("compact");

    await userEvent.click(screen.getByRole("button"));
    expect(document.documentElement.getAttribute("data-density")).toBe("comfortable");
    expect(localStorage.getItem("merchantops.density")).toBeNull();
  });

  it("announces the state it is in, not just an icon", () => {
    render(<DensityToggle />);
    expect(screen.getByRole("button"))
      .toHaveAccessibleName(/Density: comfortable\. Switch to compact\./);
  });
});

describe("a search hit cannot navigate off-site", () => {
  // React Router 6 carries an open-redirect advisory for backslashes reaching
  // `<Link>` and `useNavigate`. The upstream fix is a breaking major; the
  // exposure here is one function wide, so it is closed here.
  it("accepts the routes the server actually builds", () => {
    for (const route of ["/payments/SYN_PAY_0002", "/incidents/INC_1",
                         "/tasks/TASK_A", "/actions", "/actions?section=unknown"]) {
      expect(internalRoute(route)).toBe(route);
    }
  });

  it("refuses anything that could leave the origin", () => {
    for (const hostile of [
      "//evil.example.com",          // protocol-relative
      "https://evil.example.com",    // absolute
      "\\\\evil.example.com",            // backslashes — the advisory's vector
      "/\\evil.example.com",
      "javascript:alert(1)",
      "",
    ]) {
      // Nowhere, rather than somewhere. A refused route lands on the home
      // screen, which is a place the operator can see they are.
      expect(internalRoute(hostile), hostile).toBe("/");
    }
  });
});
