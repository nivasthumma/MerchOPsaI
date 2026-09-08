import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";
import App from "./App";
import { AppErrorBoundary } from "./components/ErrorBoundary";
import Actions from "./routes/Actions";
import CommandCenter from "./routes/CommandCenter";
import Dashboard from "./routes/Dashboard";
import IncidentDetail from "./routes/IncidentDetail";
import Incidents from "./routes/Incidents";
import Investigate from "./routes/Investigate";
import TaskDetail from "./routes/TaskDetail";
import Scenarios from "./routes/Scenarios";
import Operations from "./routes/Operations";
import PaymentLifecycle from "./routes/PaymentLifecycle";
import Recovery from "./routes/Recovery";
import Settings from "./routes/Settings";
import "./styles.css";

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    errorElement: <AppErrorBoundary />,
    children: [
      // The Command Center is the home screen — plan P0-05. A merchant
      // opening this application is asking "what needs my attention?", and
      // the answer used to be a free-text box.
      { index: true, element: <CommandCenter /> },
      { path: "investigate", element: <Investigate /> },
      { path: "actions", element: <Actions /> },
      { path: "recovery", element: <Recovery /> },
      { path: "dashboard", element: <Dashboard /> },
      { path: "incidents", element: <Incidents /> },
      { path: "incidents/:incidentId", element: <IncidentDetail /> },
      { path: "tasks/:taskId", element: <TaskDetail /> },
      // MerchantOps §7. Where a searched payment, order, customer or
      // provider reference lands — the one page that answers "what
      // happened to this payment".
      { path: "payments/:paymentId", element: <PaymentLifecycle /> },
      { path: "scenarios", element: <Scenarios /> },
      { path: "operations", element: <Operations /> },
      { path: "settings", element: <Settings /> },
      // Signed out this renders the sign-in page and signed in it
      // redirects home, both handled in `App`. It needs to exist here
      // regardless: without a route the path falls to the error
      // boundary and a signed-out visitor clicking "Sign in" gets a 404.
      { path: "signin", element: <CommandCenter /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
