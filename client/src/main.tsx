import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { RootApp } from "./RootApp";

const root = document.getElementById("root");

if (!root) {
  throw new Error("找不到应用挂载节点");
}

createRoot(root).render(
  <StrictMode>
    <RootApp />
  </StrictMode>,
);
