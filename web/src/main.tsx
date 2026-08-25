import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import App from "./App";
import { AppErrorBoundary } from "./components/ErrorBoundary";
import Investigate from "./routes/Investigate";
import TaskDetail from "./routes/TaskDetail";
import Scenarios from "./routes/Scenarios";
import Operations from "./routes/Operations";
import Settings from "./routes/Settings";
import "./styles.css";

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    errorElement: <AppErrorBoundary />,
    children: [
      { index: true, element: <Investigate /> },
      { path: "tasks/:taskId", element: <TaskDetail /> },
      { path: "scenarios", element: <Scenarios /> },
      { path: "operations", element: <Operations /> },
      { path: "settings", element: <Settings /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
