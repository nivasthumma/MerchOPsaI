// What `aria-modal="true"` promises — P1-12.
//
// Two dialogs depend on this hook, so its rules are tested here rather than
// only through whichever dialog happens to exercise them. Each test is named
// after one half of the promise.

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { useModalFocus } from "./useModalFocus";

/** A minimal dialog: an opener outside it, three controls inside. */
function Harness({ onClose = () => {}, focusInput = false, empty = false }: {
  onClose?: () => void; focusInput?: boolean; empty?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const panel = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);

  useModalFocus(panel, {
    onClose: () => { setOpen(false); onClose(); },
    initial: focusInput ? input : undefined,
    active: open,
  });

  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      <button>Outside</button>
      {open ? (
        <div role="dialog" aria-modal="true" aria-label="Test dialog"
             ref={panel} tabIndex={-1}>
          {empty ? null : (
            <>
              <input ref={input} aria-label="Query" />
              <button>First</button>
              <button>Last</button>
            </>
          )}
        </div>
      ) : null}
    </>
  );
}

async function open(props = {}) {
  render(<Harness {...props} />);
  const opener = screen.getByRole("button", { name: "Open" });
  await userEvent.click(opener);
  return opener;
}

describe("focus moves in", () => {
  it("focuses the panel itself by default", async () => {
    await open();
    // Not the first control. A dialog whose first control is Close would
    // announce "close" before saying what was opened.
    expect(document.activeElement).toBe(screen.getByRole("dialog"));
  });

  it("focuses a nominated element when the caller names one", async () => {
    await open({ focusInput: true });
    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Query" }));
  });
});

describe("Tab stays in", () => {
  it("wraps forwards at the last control", async () => {
    await open();
    screen.getByRole("button", { name: "Last" }).focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Query" }));
  });

  it("wraps backwards at the first control", async () => {
    await open();
    screen.getByRole("textbox", { name: "Query" }).focus();
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Last" }));
  });

  it("never reaches a control outside the dialog", async () => {
    await open();
    const outside = screen.getByRole("button", { name: "Outside" });
    for (let i = 0; i < 6; i++) await userEvent.tab();
    expect(document.activeElement).not.toBe(outside);
    expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
  });

  it("holds focus even in a dialog with nothing focusable in it", async () => {
    // An empty dialog with no trap lets the very first Tab escape, which is
    // the worst version of this bug: nothing looks wrong until it happens.
    await open({ empty: true });
    await userEvent.tab();
    expect(document.activeElement).toBe(screen.getByRole("dialog"));
  });
});

describe("the way out", () => {
  it("closes on Escape", async () => {
    const onClose = vi.fn();
    await open({ onClose });
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("returns focus to whatever opened it", async () => {
    const opener = await open();
    await userEvent.keyboard("{Escape}");
    // Without this, focus lands on <body> and the next Tab starts again from
    // the top of the page.
    expect(document.activeElement).toBe(opener);
  });

  it("does nothing while it is not active", async () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    await userEvent.keyboard("{Escape}");
    // A closed dialog must not be listening. Otherwise every Escape anywhere
    // in the app runs a close handler for something that is not on screen.
    expect(onClose).not.toHaveBeenCalled();
  });
});
