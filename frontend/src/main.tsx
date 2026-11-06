import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { forgetOidcLeftovers } from "./lib/oidcLeftovers";
import "./index.css";

// Before anything renders: what the Keycloak console left in this tab's
// storage goes (`lib/oidcLeftovers.ts`).
forgetOidcLeftovers();

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
