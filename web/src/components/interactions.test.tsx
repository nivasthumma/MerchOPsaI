// The interactive layer added in the visual pass. Two of these encode
// judgements rather than mechanics: a success toast may disappear on its own,
// a refusal may not, and the stepper must show a halted task as halted rather
// than as progressing.

import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DensityToggle } from "./Chrome";
import { CommandPalette } from "./CommandPalette";
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

  it("opens on ⌘K and closes on Escape", () => {
    open();
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
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
