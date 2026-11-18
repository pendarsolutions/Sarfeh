import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { LangProvider } from "./lang";
import { Prices } from "./Prices";
import "@pendar/tokens.css";
import "./site.css";

const TITLES = ["قیمت سرور ابری در ایران، کنار هم", "Iranian cloud server prices, side by side"] as const;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <LangProvider titles={TITLES}>
      <Prices />
    </LangProvider>
  </StrictMode>,
);
