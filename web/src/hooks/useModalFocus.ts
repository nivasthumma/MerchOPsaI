// What `aria-modal="true"` actually promises — P1-12.
//
// Declaring `role="dialog" aria-modal="true"` tells assistive technology that
// everything outside this element is inert. Three behaviours have to be true
// for that to be honest, and none of them is free:
//
//   focus moves in      or a screen reader is still reading the page behind
//   Tab stays in        or the user walks into content the dialog said was gone
//   Escape closes       the expected way out of any modal
//   focus returns       or closing drops focus on <body> and the next Tab
//                       starts again from the top of the page
//
// Both dialogs in this app declared the attribute and implemented some subset.
// The action drawer implemented none of it: a keyboard user could open it and
// not get out. The command palette focused its input and stopped there.
//
// This is one hook rather than two implementations for the same reason
// `useLiveRefresh` is: a rule written twice is a rule that holds in one place.

import { useEffect, type RefObject } from "react";

// What counts as focusable for the wrap. Deliberately not a library: this is
// the set this application actually renders inside a dialog, and a general
// solution would be more code guarding cases that do not occur here.
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select, textarea,'
  + ' [tabindex]:not([tabindex="-1"])';

export interface ModalFocusOptions {
  /** Called on Escape, and on nothing else. The caller decides what closing
   *  means — some dialogs clear state as well as hiding. */
  onClose: () => void;
  /** Where focus should land. Omit to focus the panel itself.
   *
   *  Worth choosing deliberately. The action drawer focuses the panel, because
   *  its first control is Close and landing there announces "close" before
   *  saying what was opened. The palette focuses its input, because typing is
   *  the entire point of opening it. */
  initial?: RefObject<HTMLElement | null>;
  /** False while the dialog is not mounted/open, so the hook can live above
   *  an early return. */
  active?: boolean;
}

export function useModalFocus(
  panel: RefObject<HTMLElement | null>,
  { onClose, initial, active = true }: ModalFocusOptions,
): void {
  useEffect(() => {
    if (!active) return;

    // Captured before focus moves, so it is genuinely the thing that opened
    // this and not something the dialog focused on the way in.
    const opener = document.activeElement as HTMLElement | null;
    (initial?.current ?? panel.current)?.focus();

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        // Stopped here: a nested dialog must not also close whatever is behind
        // it on a single press.
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panel.current) return;

      const focusable = Array.from(
        panel.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (focusable.length === 0) {
        // Nothing to move between, so Tab must not leave either.
        e.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];

      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      // `isConnected` guards the case where the opener was itself removed while
      // the dialog was open — focusing a detached node silently does nothing
      // and leaves focus on <body>, which is the state this exists to avoid.
      if (opener?.isConnected) opener.focus();
    };
  }, [active, onClose, initial, panel]);
}
