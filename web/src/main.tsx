import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import App from "./App";
import Investigate from "./routes/Investigate";
import TaskDetail from "./routes/TaskDetail";
import Scenarios from "./routes/Scenarios";
import Operations from "./routes/Operations";
import "./styles.css";

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <Investigate /> },
      { path: "tasks/:taskId", element: <TaskDetail /> },
      { path: "scenarios", element: <Scenarios /> },
      { path: "operations", element: <Operations /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
