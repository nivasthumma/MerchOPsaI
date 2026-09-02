// Does the hand-written contract still match the server? — ADR-0032
//
// `types.ts` is what this app reads. `schema.d.ts` is generated from
// `docs/openapi.json`, which the backend exports and a Python test keeps in step
// with the running application. Nothing used to connect the two, so a field
// renamed on the backend broke here at runtime, in a browser, with a green build
// behind it.
//
// These are type-level assertions with no runtime cost: `npm run typecheck`
// fails if a shape the server sends stops fitting the shape this app expects.
// Regenerate with `npm run gen:api` after `make openapi`.

import type { components } from "./schema";
import type {
  AgentAction,
  AgentMessage,
  Approval,
  FailureClass,
  Finding,
  LiveEvent,
  LiveEventList,
  RunVersions,
  Task,
} from "./types";

type Schema = components["schemas"];

/** The server's shape must fit the shape this app reads.
 *
 *  Directional on purpose: the app may declare fewer fields than the server
 *  sends — extra data is harmless — but every field it *does* declare has to
 *  exist server-side with a compatible type. The reverse check would fail on
 *  every field the UI has no use for. */
type ServerFits<Server extends Client, Client> = Server;

export type _Task = ServerFits<Schema["TaskView"], Task>;
export type _Action = ServerFits<Schema["ActionView"], AgentAction>;
export type _Approval = ServerFits<Schema["ApprovalView"], Approval>;
export type _Finding = ServerFits<Schema["FindingView"], Finding>;
export type _Versions = ServerFits<Schema["RunVersions"], RunVersions>;
export type _Failure = ServerFits<Schema["FailureClassView"], FailureClass>;
export type _Message = ServerFits<Schema["MessageView"], AgentMessage>;
export type _LiveEvent = ServerFits<Schema["LiveEventView"], LiveEvent>;
export type _LiveEvents = ServerFits<Schema["LiveEventList"], LiveEventList>;
