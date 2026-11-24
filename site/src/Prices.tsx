/**
 * The public price page: one server size at every provider, from public/prices.json.
 *
 * That file is written by scripts/build_price_table.py from the price catalog (packages/pricing_engine/size_table.py),
 * and a test keeps it in step. This page only shows it: every amount, and even "4.8 times the cheapest", arrives
 * worked out, so nothing here does arithmetic on a price.
 */
import { useEffect, useMemo, useState } from "react";
import { Lockup, PoweredBy } from "./Brand";
import { Icon } from "./Icon";
import { formatWhole, localizeDigits } from "./format";
import type { Lang } from "./lang";
import { CATALOG, CPU_MODEL, HOME, LICENSE, PRICES, PRICES_JSON, PRICING_MODEL, REPO, WRONG_PRICE } from "./links";
import { useT, type Key } from "./strings";

interface Offer {
  provider_id: string;
  kind: "plan" | "components" | "by_size";
  plan: string | null;
  plan_name: string | null;
  monthly_toman: string;
  vat_toman: string | null;
  times_cheapest: string;
  cpu: string;
  ram_gb: string;
  disk_gb: string;
  cpu_shared: boolean | null;
  disk_type: string | null;
  traffic: string;
  included_traffic_gb: string | null;
  datacenter: string | null;
  source_url: string | null;
  read_on: string | null;
  /** what a reader should know about the figure, by id; older tables have none */
  conditions?: Condition[];
  /** set by the table when the row was read too long ago: how many days counts as old */
  old_after_days?: number;
}
interface Condition {
  id: string;
  toman?: string;
  days?: number;
}
interface Missing {
  provider_id: string;
  reason: "starting_price_only" | "nothing_this_size";
}
interface Size {
  cpu: number;
  ram_gb: number;
  disk_gb: number;
  offers: Offer[];
  missing: Missing[];
}
interface Table {
  schema: number;
  catalog_date: string;
  providers: Record<string, { name_fa: string; name_en: string; website: string }>;
  sizes: Size[];
}
type Want = { cpu: number | null; ram: number | null; disk: number | null };

const TRAFFIC: Record<string, Key> = {
  included_quota: "prTrafficQuota",
  fair_use: "prTrafficFair",
  unlimited: "prTrafficUnlimited",
  unlimited_rate_limited: "prTrafficLimited",
  metered: "prTrafficMetered",
  metered_download: "prTrafficMetered",
};
const DISK_TYPE: Record<string, string> = { ssd: "SSD", hot_ssd: "SSD", nvme: "NVMe", hdd: "HDD" };
/** A condition in the reader's language, or nothing for an id this page doesn't know yet. */
function conditionText(c: Condition, t: (k: Key) => string, lang: Lang): string | null {
  if (c.id === "ipv4_assumed") return t("prCondIpv4Assumed");
  if (c.id === "ipv4_priced" && c.toman) return fill(t("prCondIpv4Priced"), { amount: formatWhole(c.toman, lang) });
  if (c.id === "thirty_day_plan") return t("prCondThirtyDay");
  if (c.id === "setup_fee" && c.days) return fill(t("prCondSetupFee"), { n: localizeDigits(String(c.days), lang) });
  return null;
}

const MISSING: Record<Missing["reason"], Key> = { starting_price_only: "prMissingStart", nothing_this_size: "prMissingSize" };

const uniq = (xs: number[]) => [...new Set(xs)].sort((a, b) => a - b);
const fill = (s: string, vars: Record<string, string>) => s.replace(/\{(\w+)\}/g, (_, k: string) => vars[k] ?? "");

/** A whole number from the address, so a comparison can be linked to: /prices/?cpu=4&ram=8&disk=100 */
function fromAddress(name: string): number | null {
  const v = Number(new URLSearchParams(window.location.search).get(name));
  return Number.isInteger(v) && v > 0 ? v : null;
}

/** The size in the table nearest to what was asked: a memory or disk the chosen vCPUs aren't sold with falls back. */
function settle(table: Table, want: Want): Size {
  const cpus = uniq(table.sizes.map((s) => s.cpu));
  const cpu = want.cpu !== null && cpus.includes(want.cpu) ? want.cpu : cpus.includes(2) ? 2 : cpus[0];
  const rams = uniq(table.sizes.filter((s) => s.cpu === cpu).map((s) => s.ram_gb));
  const ram = want.ram !== null && rams.includes(want.ram) ? want.ram : rams[Math.floor(rams.length / 2)];
  const disks = uniq(table.sizes.filter((s) => s.cpu === cpu && s.ram_gb === ram).map((s) => s.disk_gb));
  const disk = want.disk !== null && disks.includes(want.disk) ? want.disk : disks.includes(50) ? 50 : disks[0];
  return table.sizes.find((s) => s.cpu === cpu && s.ram_gb === ram && s.disk_gb === disk)!;
}

/** A date as its reader writes it: Solar Hijri in Persian. */
function day(iso: string, lang: Lang): string {
  const d = new Date(`${iso.slice(0, 10)}T12:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso;
  const locale = lang === "fa" ? "fa-IR-u-ca-persian" : "en-GB";
  return new Intl.DateTimeFormat(locale, { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" }).format(d);
}

function host(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function Choice({ legend, name, options, value, onPick }: { legend: string; name: string; options: number[]; value: number; onPick: (v: number) => void }) {
  const { lang } = useT();
  return (
    <fieldset>
      <legend>{legend}</legend>
      <div className="pr-chips">
        {options.map((o) => (
          <label key={o} className="pr-chip">
            <input type="radio" name={name} value={o} checked={o === value} onChange={() => onPick(o)} />
            <span>{localizeDigits(String(o), lang)}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function OfferRow({ offer, rank, size, table }: { offer: Offer; rank: number; size: Size; table: Table }) {
  const { t, lang } = useT();
  const n = (digits: string) => localizeDigits(digits, lang);
  const provider = table.providers[offer.provider_id];
  const bigger = Number(offer.cpu) > size.cpu || Number(offer.ram_gb) > size.ram_gb || Number(offer.disk_gb) > size.disk_gb;
  const plan =
    offer.kind === "components" ? fill(t("prBuilt"), { cls: offer.plan ?? "" }) : offer.kind === "by_size" ? t("prBySize") : (offer.plan_name ?? offer.plan ?? "");
  const diskType = offer.disk_type ? DISK_TYPE[offer.disk_type] : undefined;
  const trafficKey = TRAFFIC[offer.traffic] ?? "prTrafficUnknown";
  const times = n(offer.times_cheapest).replace(".", lang === "fa" ? "٫" : ".");
  const conditions = (offer.conditions ?? []).map((c) => conditionText(c, t, lang)).filter((text): text is string => text !== null);
  return (
    <li className="pr-offer">
      <span className="pr-rank num" aria-hidden>
        {n(String(rank))}
      </span>
      <div className="pr-what">
        <h3 className="pr-name">{lang === "fa" ? provider.name_fa : provider.name_en}</h3>
        <p className="pr-plan">
          <bdi>{plan}</bdi>
        </p>
        {/* each part in its own <bdi>, so a Latin unit never pulls a neighbouring Persian number out of place */}
        <p className="pr-specs">
          <bdi>
            {n(offer.cpu)} {t("prCores")}
          </bdi>
          <bdi>
            {n(offer.ram_gb)} {t("prGb")} {t("prMemory")}
          </bdi>
          <bdi>
            {n(offer.disk_gb)} {t("prGb")} {diskType ?? t("prDisk")}
          </bdi>
          {bigger && <span className="badge badge-info">{t("prBigger")}</span>}
        </p>
        <ul className="pr-terms">
          <li>{t(offer.cpu_shared === null ? "prCpuUnknown" : offer.cpu_shared ? "prCpuShared" : "prCpuDedicated")}</li>
          <li>{fill(t(trafficKey), { n: formatWhole(offer.included_traffic_gb ?? "", lang) })}</li>
          {offer.datacenter && (
            <li>
              {t("prDatacenter")}: <bdi>{offer.datacenter}</bdi>
            </li>
          )}
        </ul>
        {conditions.length > 0 && (
          <ul className="pr-conditions">
            {conditions.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        )}
      </div>
      <div className="pr-cost">
        <strong className="pr-price">
          {formatWhole(offer.monthly_toman, lang)} <small>{t("prPerMonth")}</small>
        </strong>
        {offer.vat_toman && <span className="note">{fill(t("prVat"), { amount: formatWhole(offer.vat_toman, lang) })}</span>}
        {rank === 1 ? <span className="badge badge-good">{t("prCheapest")}</span> : <span className="note">{fill(t("prTimes"), { x: times })}</span>}
      </div>
      {offer.source_url && (
        <p className="pr-source">
          {t("prSource")}:{" "}
          <a href={offer.source_url} rel="noopener noreferrer" dir="ltr">
            {host(offer.source_url)}
          </a>
          {offer.read_on && <> · {fill(t("prReadOn"), { date: day(offer.read_on, lang) })}</>}
          {offer.old_after_days !== undefined && (
            <>
              {" "}
              <span className="badge badge-warn" title={fill(t("prOldTitle"), { n: localizeDigits(String(offer.old_after_days), lang) })}>
                {t("prOld")}
              </span>
            </>
          )}
        </p>
      )}
    </li>
  );
}

function Comparison({ table }: { table: Table }) {
  const { t, lang } = useT();
  const [want, setWant] = useState<Want>(() => ({ cpu: fromAddress("cpu"), ram: fromAddress("ram"), disk: fromAddress("disk") }));
  const size = useMemo(() => settle(table, want), [table, want]);
  const cpus = useMemo(() => uniq(table.sizes.map((s) => s.cpu)), [table]);
  const rams = uniq(table.sizes.filter((s) => s.cpu === size.cpu).map((s) => s.ram_gb));
  const disks = uniq(table.sizes.filter((s) => s.cpu === size.cpu && s.ram_gb === size.ram_gb).map((s) => s.disk_gb));
  const n = (v: number) => localizeDigits(String(v), lang);

  // the address always names the comparison on screen, so copying it shares exactly this one
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    params.set("cpu", String(size.cpu));
    params.set("ram", String(size.ram_gb));
    params.set("disk", String(size.disk_gb));
    window.history.replaceState(null, "", `?${params}`);
  }, [size]);

  const pick = (part: Partial<Want>) => setWant({ cpu: size.cpu, ram: size.ram_gb, disk: size.disk_gb, ...part });
  const name = (id: string) => (lang === "fa" ? table.providers[id].name_fa : table.providers[id].name_en);

  return (
    <>
      <form className="pr-picker" aria-label={t("prPickerLabel")} onSubmit={(e) => e.preventDefault()}>
        <Choice legend={t("prPickCpu")} name="cpu" options={cpus} value={size.cpu} onPick={(cpu) => pick({ cpu })} />
        <Choice legend={t("prPickRam")} name="ram" options={rams} value={size.ram_gb} onPick={(ram) => pick({ ram })} />
        <Choice legend={t("prPickDisk")} name="disk" options={disks} value={size.disk_gb} onPick={(disk) => pick({ disk })} />
      </form>

      <section className="pr-results" aria-live="polite">
        <h2>{fill(t("prResults"), { n: n(size.offers.length), cpu: n(size.cpu), ram: n(size.ram_gb), disk: n(size.disk_gb) })}</h2>
        <ol className="pr-offers">
          {size.offers.map((o, i) => (
            <OfferRow key={o.provider_id} offer={o} rank={i + 1} size={size} table={table} />
          ))}
        </ol>
        <p className="note pr-scope">{t("prScope")}</p>
        {size.missing.length > 0 && (
          <div className="pr-missing">
            <h3>{t("prMissingTitle")}</h3>
            <ul>
              {size.missing.map((m) => (
                <li key={m.provider_id}>
                  <strong>{name(m.provider_id)}:</strong> {t(MISSING[m.reason])}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </>
  );
}

export function Prices() {
  const { t, lang, setLang } = useT();
  const [table, setTable] = useState<Table | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    fetch(PRICES_JSON)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((data: Table) => live && setTable(data))
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, []);

  return (
    <div className="lp" id="top">
      <header className="lp-nav">
        <a href={HOME} className="brand brand-link" aria-label={t("appName")}>
          <Lockup large />
        </a>
        <nav className="lp-links" aria-label={lang === "fa" ? "صفحه‌ها" : "Pages"}>
          <a href={PRICES} aria-current="page">
            {t("prNav")}
          </a>
          <a href={HOME}>{t("prNavTool")}</a>
        </nav>
        <div className="lp-actions">
          <button type="button" className="btn btn-ghost" onClick={() => setLang(lang === "fa" ? "en" : "fa")} lang={lang === "fa" ? "en" : "fa"}>
            {lang === "fa" ? "EN" : "فا"}
          </button>
          <a className="btn btn-ghost" href={REPO}>
            GitHub
          </a>
        </div>
      </header>

      <main className="pr-main">
        <div className="pr-head">
          <h1>{t("prTitle")}</h1>
          <p>{t("prLead")}</p>
          {table && <p className="note">{fill(t("prCatalogDate"), { date: day(table.catalog_date, lang) })}</p>}
        </div>

        {table ? <Comparison table={table} /> : <p role="status">{t(failed ? "prError" : "prLoading")}</p>}

        <section className="pr-method">
          <h2>{t("prMethodTitle")}</h2>
          <ul>
            {(["prMethod1", "prMethod2", "prMethod3", "prMethod4"] as Key[]).map((k) => (
              <li key={k}>{t(k)}</li>
            ))}
          </ul>
          <ul className="pr-links">
            <li>
              <a href={WRONG_PRICE}>
                {t("prWrong")} <Icon name="external" />
              </a>
            </li>
            <li>
              <a href={CPU_MODEL}>
                {t("prCpuReport")} <Icon name="external" />
              </a>
            </li>
            <li>
              <a href={PRICES_JSON} download>
                {t("prData")}
              </a>
            </li>
            <li>
              <a href={CATALOG}>
                {t("prCatalog")} <Icon name="external" />
              </a>
            </li>
            <li>
              <a href={PRICING_MODEL}>
                {t("prHow")} <Icon name="external" />
              </a>
            </li>
          </ul>
        </section>

        <section className="pr-cta">
          <div>
            <h2>{t("prCtaTitle")}</h2>
            <p>{t("prCtaBody")}</p>
          </div>
          <a className="btn btn-primary" href={HOME}>
            {t("prCtaButton")}
          </a>
        </section>
      </main>

      <footer className="lp-footer">
        <span>{t("lpFooter")}</span>
        <a href={REPO}>GitHub</a>
        <a href={LICENSE}>{t("lpLicense")}</a>
        <span>© {localizeDigits("2026", lang)}</span>
        <PoweredBy />
      </footer>
    </div>
  );
}
