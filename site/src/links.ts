/** Where the site sends people. Change INSTALL_URL to this site's own /install.sh once it serves one. */
export const REPO = "https://github.com/pendarsolutions/Sarfeh";
export const RELEASES = `${REPO}/releases`;
export const GUIDE = `${REPO}/blob/main/docs/installation.md`;
export const LICENSE = `${REPO}/blob/main/LICENSE`;
export const INSTALL_URL = `${REPO}/releases/latest/download/install.sh`;
export const INSTALL_COMMAND = `curl -fsSL ${INSTALL_URL} | sudo bash`;

/** The site's own pages and files, wherever it is served from. */
const BASE = import.meta.env.BASE_URL;
export const HOME = BASE;
export const PRICES = `${BASE}prices/`;
export const PRICES_JSON = `${BASE}prices.json`;
export const WRONG_PRICE = `${REPO}/issues/new?template=wrong-price.md`;
export const CPU_MODEL = `${REPO}/issues/new?template=cpu-model.md`;
export const PRICING_MODEL = `${REPO}/blob/main/docs/pricing-model.md`;
export const CATALOG = `${REPO}/tree/main/data/seeds`;
