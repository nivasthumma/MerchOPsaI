// One payment, end to end — MerchantOps §7.
//
// The fixture is a live `/payments/{id}/lifecycle` response, captured after
// driving a payment all the way through: detection, investigation, the policy
// gate, approval, execution, verification and a provider event.

import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PaymentLifecycle as Data } from "../api/types";
import PaymentLifecycle from "./PaymentLifecycle";
import fixture from "../test-fixtures/payment-lifecycle.json";

vi.mock("react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router")>();
  return { ...actual, useParams: () => ({ paymentId: "SYN_PAY_0002" }) };
});
vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { paymentLifecycle: vi.fn() } };
});

const { api } = await import("../api/client");
const data = fixture as unknown as Data;

const renderPage = async () => {
  render(<MemoryRouter><PaymentLifecycle /></MemoryRouter>);
  await screen.findByText("Lifecycle");
};

beforeEach(() => {
  vi.mocked(api.paymentLifecycle).mockReset().mockResolvedValue(data);
});

describe("the chain", () => {
  it("renders every event the server found, in the server's order", async () => {
    await renderPage();
    const items = within(screen.getByRole("list", { name: "Payment lifecycle" }))
      .getAllByRole("listitem");
    expect(items).toHaveLength(data.events.length);
    expect(items[0]).toHaveTextContent(data.events[0].label);
  });

  it("does not reorder events into the sequence they usually occur in", async () => {
    // An out-of-order provider event really did arrive out of order, and
    // tidying it into place hides the thing worth seeing. The page renders the
    // array as given.
    const reversed = { ...data, events: [...data.events].reverse() };
    vi.mocked(api.paymentLifecycle).mockResolvedValue(reversed);
    await renderPage();

    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent(reversed.events[0].label);
  });

  it("marks each stage with a shape, not only a colour", async () => {
    await renderPage();
    const items = screen.getAllByRole("listitem");
    const stages = items.map((li) => li.getAttribute("data-stage"));
    expect(new Set(stages).size).toBeGreaterThan(3);
    // The stage name reaches a screen reader; the glyph is decoration.
    expect(screen.getAllByText(/\(verification\)/).length).toBeGreaterThan(0);
  });

  it("says a step with no honest timestamp has none", async () => {
    vi.mocked(api.paymentLifecycle).mockResolvedValue({
      ...data,
      events: [{ stage: "policy", at: null, id: "X", label: "Policy evaluated",
                 detail: "", correlation_id: null }],
    });
    await renderPage();
    expect(screen.getByTitle(/no honest timestamp/)).toBeInTheDocument();
  });

  it("does not report a policy-gated call as failed", async () => {
    await renderPage();
    // The fixture contains a real gated `request_refund` next to a refund that
    // went on to succeed.
    expect(screen.queryByText("failed")).toBeNull();
    expect(screen.getByText(/held by policy/)).toBeInTheDocument();
  });
});

describe("what the page claims", () => {
  it("counts the correlations, because more than one is why it exists", async () => {
    await renderPage();
    expect(data.correlation_ids.length).toBeGreaterThan(1);
    expect(screen.getByText(/No single trace covers it/)).toBeInTheDocument();
  });

  it("links out to the incident, the investigation and the actions", async () => {
    await renderPage();
    for (const id of data.incident_ids) {
      expect(screen.getByRole("link", { name: id }))
        .toHaveAttribute("href", `/incidents/${id}`);
    }
    for (const id of data.task_ids) {
      expect(screen.getByRole("link", { name: id }))
        .toHaveAttribute("href", `/tasks/${id}`);
    }
  });

  it("says an unmapped payment cannot be executed against, not that data is missing", async () => {
    vi.mocked(api.paymentLifecycle).mockResolvedValue({
      ...data, external_payment_id: null, provider: null, environment: null,
    });
    await renderPage();
    // "—" would read as missing data. This is the mapping layer working.
    expect(screen.getByText(/cannot be executed against externally/))
      .toBeInTheDocument();
  });

  it("shows the provider environment beside the reference", async () => {
    await renderPage();
    expect(screen.getByText(/razorpay · test/)).toBeInTheDocument();
  });
});
