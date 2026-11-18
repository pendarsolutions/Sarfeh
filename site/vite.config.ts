import { fileURLToPath } from "node:url";
import { defineConfig, type Connect, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

// Pendar's design system as scripts/pull-pendar.sh generates it into the app (tokens, font, icons, logo), so the site
// and the app always match. If the site moves to its own repository, copy that folder and point this alias at it.
const pendar = fileURLToPath(new URL("../apps/web/src/pendar", import.meta.url));

const page = (path: string) => fileURLToPath(new URL(path, import.meta.url));

/** /prices without its last slash goes to the price page, as static hosts do for a folder. Left alone, Vite's own
 *  servers (npm run dev, npm run preview) answer it with the home page. */
const toPrices: Connect.NextHandleFunction = (req, res, next) => {
  const [path, query] = (req.url ?? "").split("?");
  if (path !== "/prices") return next();
  res.writeHead(302, { Location: `/prices/${query ? `?${query}` : ""}` });
  res.end();
};
const pricesSlash: Plugin = {
  name: "prices-trailing-slash",
  configureServer: (server) => void server.middlewares.use(toPrices),
  configurePreviewServer: (server) => void server.middlewares.use(toPrices),
};

export default defineConfig({
  // where the site is served: "/" by default, or a folder such as SITE_BASE=/sarfeh/ for pendarsolutions.ir/sarfeh/
  base: process.env.SITE_BASE ?? "/",
  plugins: [react(), pricesSlash],
  // two pages: the home page, and the price comparison at /prices/
  build: { rollupOptions: { input: { home: page("index.html"), prices: page("prices/index.html") } } },
  resolve: { alias: { "@pendar": pendar } },
  server: { port: 5174, fs: { allow: [fileURLToPath(new URL("..", import.meta.url))] } },
});
