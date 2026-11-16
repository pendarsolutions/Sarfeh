import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { SpendEntry, SpendEntryInput, SpendGap, SpendMonth, SpendSummary } from "../api/types";
import { useI18n, type TKey } from "../lib/i18n";
import { useInventory, useProviderName, useProviders } from "../lib/hooks";
import { useSession } from "../lib/session";
import { formatDate, formatDecimal, formatPercent, formatToman, localizeDigits } from "../lib/money";
import { monthName, monthShort, recentMonths } from "../lib/months";
import { providerColor } from "../lib/viz";
import { MonthColumns, type ColumnMonth } from "../components/viz";
import { Rec, RecList } from "../components/records";
import { Button, Card, Drawer, Empty, ErrorBox, Field, Loading, Money, StatusChip, type Tone } from "../components/ui";
import { Icon } from "../components/Icon";

const OTHER = "var(--series-other)";
const SOURCE_LABEL: Record<string, TKey> = {
  manual: "spendSrcManual",
  wallet_topup: "spendSrcWallet",
  invoice: "spendSrcInvoice",
  csv: "spendSrcCsv",
  sample: "spendSrcSample",
};

/** Persian or Latin digits, with or without separators, to a plain decimal string ("" if it isn't one). */
function plainAmount(text: string): string {
  const latin = text
    .replace(/[۰-۹]/g, (d) => String("۰۱۲۳۴۵۶۷۸۹".indexOf(d)))
    .replace(/[٠-٩]/g, (d) => String("٠١٢٣٤٥٦٧٨٩".indexOf(d)))
    .replace(/٫/g, ".")
    .replace(/[,٬\s]/g, "");
  return /^\d+(\.\d+)?$/.test(latin) ? latin : "";
}

function payeeColor(key: string, providerId: string | null): string {
  return providerId ? providerColor(providerId).css : key.startsWith("vendor:") ? OTHER : providerColor(key).css;
}

/** Order payees as every chart orders providers: by their color slot, vendors last. */
function payeeOrder(a: { key: string; provider_id: string | null }, b: { key: string; provider_id: string | null }) {
  const slot = (p: { provider_id: string | null }) => providerColor(p.provider_id).slot ?? 99;
  return slot(a) - slot(b) || a.key.localeCompare(b.key);
}

const unsigned = (v: string) => v.replace(/^-/, "");

/** A change as an arrow and an amount, without a + or − sign, which reads ambiguously beside Persian text. */
function Change({ amount, percent }: { amount: string | null; percent?: string | null }) {
  const { t, lang } = useI18n();
  if (amount === null) return <span className="muted">—</span>;
  const down = amount.startsWith("-");
  const flat = amount === "0";
  return (
    <span className="change">
      {flat ? null : <Icon name={down ? "arrow-down" : "arrow-up"} small label={down ? t("spendDown") : t("spendUp")} />}
      <span className="num">{formatToman(unsigned(amount), lang)}</span>
      {percent !== undefined && percent !== null && !flat ? <span className="muted">({formatPercent(unsigned(percent), lang)})</span> : null}
    </span>
  );
}

function ChangeLine({ m }: { m: SpendMonth }) {
  const { t } = useI18n();
  if (m.change_toman === null) return null;
  return (
    <small className="muted">
      <Change amount={m.change_toman} percent={m.change_percent} /> {t("spendChangeVsPrev")}
    </small>
  );
}

export function GapChip({ gap }: { gap: SpendGap | null }) {
  const { t, lang } = useI18n();
  if (!gap) return null;
  const pct = gap.percent ? formatPercent(gap.percent.replace("-", ""), lang) : "";
  const map: Record<string, [Tone, string]> = {
    paid_more: ["warn", t("gapPaidMore").replace("{p}", pct)],
    paid_less: ["info", t("gapPaidLess").replace("{p}", pct)],
    not_paid: ["info", t("gapNotPaid")],
    not_in_setup: ["warn", t("gapNotInSetup")],
  };
  const [tone, label] = map[gap.kind] ?? ["neutral", gap.kind];
  return <StatusChip tone={tone} label={label} title={t("gapHint")} />;
}

function Estimate({ m }: { m: SpendMonth }) {
  const { t } = useI18n();
  if (m.estimated_toman === null) return <span className="muted">—</span>;
  return (
    <span title={m.estimate_complete ? undefined : t("spendIncompleteHint")}>
      {m.estimate_complete ? null : <small className="muted">{t("spendAtLeast")} </small>}
      <Money v={m.estimated_toman} />
    </span>
  );
}

// ------------------------------------------------------------------ entry form

type Draft = {
  month: string;
  payee: string; // a provider id, or "" with `vendor`
  vendor: string;
  amount: string;
  currency: "TOMAN" | "RIAL" | "";
  source: "manual" | "wallet_topup";
  paid_on: string;
  reference: string;
  note: string;
};

function draftOf(e: SpendEntry | null, month: string): Draft {
  if (!e) return { month, payee: "", vendor: "", amount: "", currency: "TOMAN", source: "manual", paid_on: "", reference: "", note: "" };
  const known = e.amount_toman !== null;
  const detected = e.evidence.detected_currency;
  return {
    month: e.month,
    payee: e.provider_id ?? (e.vendor ? "__other" : ""),
    vendor: e.vendor ?? "",
    amount: known ? e.amount_toman! : e.evidence.total ?? "",
    // an amount read off the invoice in an unknown unit has to be placed by a person
    currency: known ? "TOMAN" : detected === "RIAL" || detected === "TOMAN" ? detected : "",
    source: e.source === "wallet_topup" ? "wallet_topup" : "manual",
    paid_on: e.paid_on ?? "",
    reference: e.reference ?? "",
    note: e.note ?? "",
  };
}

function EntryDrawer({ entry, month, current, onClose }: { entry: SpendEntry | null; month: string; current: string; onClose: () => void }) {
  const { t, lang } = useI18n();
  const qc = useQueryClient();
  const providers = useProviders();
  const [d, setD] = useState<Draft>(() => draftOf(entry, month));
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD((x) => ({ ...x, [k]: v }));
  const reviewing = entry?.status === "needs_review";
  const fixedSource = entry && !["manual", "wallet_topup"].includes(entry.source);
  const months = useMemo(() => {
    const list = recentMonths(current, 36);
    return list.includes(d.month) ? list : [d.month, ...list];
  }, [current, d.month]);

  const done = () => {
    qc.invalidateQueries({ queryKey: ["spend"] });
    qc.invalidateQueries({ queryKey: ["spendEntries"] });
    onClose();
  };
  const save = useMutation({
    mutationFn: (confirm: boolean) => {
      const amount = plainAmount(d.amount);
      const body: SpendEntryInput = {
        month: d.month,
        provider_id: d.payee && d.payee !== "__other" ? d.payee : null,
        vendor: d.payee === "__other" ? d.vendor.trim() || null : null,
        amount: amount || null,
        currency: d.currency || "TOMAN",
        paid_on: d.paid_on.trim() || null,
        reference: d.reference.trim() || null,
        note: d.note.trim() || null,
      };
      if (!fixedSource) body.source = d.source;
      if (!entry) return api.addSpend(body);
      return api.patchSpend(entry.id, confirm ? { ...body, confirm: true } : body);
    },
    onSuccess: done,
  });
  const del = useMutation({ mutationFn: () => api.deleteSpend(entry!.id), onSuccess: done });
  const amountOk = plainAmount(d.amount) !== "";
  const payeeOk = (d.payee && d.payee !== "__other") || (d.payee === "__other" && d.vendor.trim() !== "");
  const ready = amountOk && payeeOk && d.currency !== "";
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (ready) save.mutate(reviewing);
  };
  const ev = entry?.evidence;

  return (
    <Drawer open onClose={onClose} title={reviewing ? t("spendCheckTitle") : entry ? t("spendEditTitle") : t("spendRecord")}>
      <form className="stack" onSubmit={submit}>
        {reviewing && ev ? (
          <div className="alert alert-info stacked">
            <strong>{t("spendReadFrom")}</strong>
            {entry?.upload ? (
              <span className="row gap-s wrap">
                <Icon name="file" small />
                {entry.upload.download_url ? (
                  <a href={entry.upload.download_url} download={entry.upload.filename}>
                    <bdi>{entry.upload.filename}</bdi>
                  </a>
                ) : (
                  <bdi>{entry.upload.filename}</bdi>
                )}
              </span>
            ) : null}
            <span>
              {t("spendInvoiceTotal")}:{" "}
              {ev.total ? (
                <>
                  <span className="num">{formatDecimal(ev.total, lang)}</span>{" "}
                  {ev.detected_currency === "RIAL" ? t("rial") : ev.detected_currency === "TOMAN" ? t("toman") : "?"}
                </>
              ) : (
                "—"
              )}
            </span>
            {ev.total_evidence ? (
              <small className="muted">
                <bdi>{localizeDigits(ev.total_evidence, lang)}</bdi>
              </small>
            ) : null}
            {ev.issued ? (
              <span>
                {t("spendInvoiceDate")}: <bdi dir="ltr">{localizeDigits(ev.issued.text, lang)}</bdi>
              </span>
            ) : null}
            {!ev.total ? <span>{t("spendNoTotal")}</span> : ev.detected_currency === "UNKNOWN" ? <span>{t("spendCurrencyUnknown")}</span> : null}
          </div>
        ) : null}

        <Field label={t("spendMonth")} htmlFor="sp-month">
          <select id="sp-month" value={d.month} onChange={(e) => set("month", e.target.value)}>
            {months.map((k) => (
              <option key={k} value={k}>
                {monthName(k, lang)}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("spendProvider")} htmlFor="sp-payee">
          <select id="sp-payee" value={d.payee} onChange={(e) => set("payee", e.target.value)} required>
            <option value="">—</option>
            {providers.data?.map((p) => (
              <option key={p.id} value={p.id}>
                {lang === "fa" ? p.name_fa || p.name_en : p.name_en}
              </option>
            ))}
            <option value="__other">{t("spendVendorOther")}</option>
          </select>
        </Field>
        {d.payee === "__other" ? (
          <Field label={t("spendVendorName")} htmlFor="sp-vendor">
            <input id="sp-vendor" value={d.vendor} maxLength={200} onChange={(e) => set("vendor", e.target.value)} required />
          </Field>
        ) : null}
        <div className="grid2">
          <Field label={t("spendAmount")} htmlFor="sp-amount">
            <input id="sp-amount" inputMode="decimal" dir="ltr" value={d.amount} onChange={(e) => set("amount", e.target.value)} aria-invalid={d.amount !== "" && !amountOk} required />
          </Field>
          <Field label={t("spendCurrency")} htmlFor="sp-cur">
            <select id="sp-cur" value={d.currency} onChange={(e) => set("currency", e.target.value as Draft["currency"])} required>
              {d.currency === "" ? <option value="">—</option> : null}
              <option value="TOMAN">{t("toman")}</option>
              <option value="RIAL">{t("rial")}</option>
            </select>
          </Field>
        </div>
        {fixedSource ? null : (
          <Field label={t("spendType")} htmlFor="sp-src">
            <select id="sp-src" value={d.source} onChange={(e) => set("source", e.target.value as Draft["source"])}>
              <option value="manual">{t("spendSrcManual")}</option>
              <option value="wallet_topup">{t("spendSrcWallet")}</option>
            </select>
          </Field>
        )}
        <Field label={t("spendPaidOn")} htmlFor="sp-paid" hint={t("spendPaidOnHint")}>
          <input id="sp-paid" dir="ltr" value={d.paid_on} onChange={(e) => set("paid_on", e.target.value)} />
        </Field>
        <Field label={t("spendReference")} htmlFor="sp-ref">
          <input id="sp-ref" dir="auto" value={d.reference} maxLength={200} onChange={(e) => set("reference", e.target.value)} />
        </Field>
        <Field label={t("spendNote")} htmlFor="sp-note">
          <textarea id="sp-note" dir="auto" rows={2} value={d.note} maxLength={2000} onChange={(e) => set("note", e.target.value)} />
        </Field>
        <ErrorBox error={save.error ?? del.error} />
        <div className="row gap wrap">
          <Button type="submit" variant="primary" busy={save.isPending} disabled={!ready}>
            {reviewing ? t("spendConfirmEntry") : t("save")}
          </Button>
          {entry ? (
            <Button variant="danger" busy={del.isPending} onClick={() => window.confirm(t("spendDeleteConfirm")) && del.mutate()}>
              <Icon name="trash" /> {t("delete")}
            </Button>
          ) : null}
          <Button variant="ghost" onClick={onClose}>
            {t("cancel")}
          </Button>
        </div>
      </form>
    </Drawer>
  );
}

// ------------------------------------------------------------------ CSV import

function ImportDrawer({ onClose }: { onClose: () => void }) {
  const { t, lang } = useI18n();
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const run = useMutation({
    mutationFn: () => api.importSpend(file!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["spend"] });
      qc.invalidateQueries({ queryKey: ["spendEntries"] });
    },
  });
  const rows = run.error instanceof ApiError ? ((run.error.detail as { errors?: { row: number; message: string }[] })?.errors ?? null) : null;
  return (
    <Drawer open onClose={onClose} title={t("spendImportTitle")}>
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          if (file) run.mutate();
        }}
      >
        <p className="muted">{t("spendImportHint")}</p>
        <code className="pre" dir="ltr">
          month,provider,amount,currency,note{"\n"}1405-07,Liara,2500000,Toman,{"\n"}1405-07,ArvanCloud,91000000,Rial,wallet
        </code>
        <Field label={t("spendImportChoose")} htmlFor="sp-csv">
          <input id="sp-csv" type="file" accept=".csv,text/csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} required />
        </Field>
        {run.data ? (
          <div className="alert alert-info" role="status">
            <Icon name="check" /> {t("spendImported").replace("{n}", formatDecimal(run.data.imported, lang))}
          </div>
        ) : null}
        {rows ? (
          <div className="alert alert-bad">
            <ul>
              {rows.map((r) => (
                <li key={r.row}>
                  {t("spendRow")} {formatDecimal(r.row, lang)}: <bdi>{r.message}</bdi>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <ErrorBox error={run.error} />
        )}
        <div className="row gap">
          <Button type="submit" variant="primary" busy={run.isPending} disabled={!file}>
            {t("spendImport")}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            {t("close")}
          </Button>
        </div>
      </form>
    </Drawer>
  );
}

// ------------------------------------------------------------------ page

function MonthDetail({ m, summary, canEdit, onEdit }: { m: SpendMonth; summary: SpendSummary; canEdit: boolean; onEdit: (e: SpendEntry) => void }) {
  const { t, lang } = useI18n();
  const pname = useProviderName();
  const qc = useQueryClient();
  const { activeId } = useSession();
  const inv = useInventory(activeId);
  const entries = useQuery({ queryKey: ["spendEntries", m.month], queryFn: () => api.spendEntries(m.month) });
  const estimate = useMutation({
    mutationFn: () => api.snapshotNow(activeId!),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["spend"] }),
  });
  const name = (p: { provider_id: string | null; vendor: string | null }) => (p.provider_id ? pname(p.provider_id, lang) : p.vendor ?? "—");
  const isCurrent = m.month === summary.current_month;
  const canEstimate = canEdit && isCurrent && !!inv.data?.has_confirmed_version;

  return (
    <div className="stack">
      <Card
        title={t("spendVsEstimate").replace("{month}", monthName(m.month, lang))}
        actions={
          canEstimate ? (
            <Button busy={estimate.isPending} onClick={() => estimate.mutate()}>
              <Icon name="refresh" /> {m.snapshot ? t("spendEstimateAgain") : t("spendEstimateNow")}
            </Button>
          ) : null
        }
      >
        <div className="stack">
          {summary.inventory ? (
            <p className="small muted">
              {t("spendEstimatesFrom").replace("{name}", summary.inventory.name)}
              {m.snapshot ? (
                <>
                  {" · "}
                  {formatDate(m.snapshot.created_at, lang)}
                </>
              ) : null}
            </p>
          ) : (
            <p className="small muted">{t("spendNoInventory")}</p>
          )}
          <ErrorBox error={estimate.error} />
          {m.by_provider.length ? (
            <div className="table-wrap">
              <table className="table compact">
                <thead>
                  <tr>
                    <th>{t("spendProvider")}</th>
                    <th className="num-col">{t("spendPaid")}</th>
                    <th className="num-col">{t("spendEstimateKey")}</th>
                    <th>{t("spendDiff")}</th>
                  </tr>
                </thead>
                <tbody>
                  {[...m.by_provider].sort(payeeOrder).map((p) => (
                    <tr key={p.key}>
                      <td>
                        <span className="row gap-s nowrap">
                          <i className="swatch" style={{ background: payeeColor(p.key, p.provider_id) }} aria-hidden /> <bdi>{name(p)}</bdi>
                        </span>
                      </td>
                      <td className="num-col">{p.paid_toman !== null ? <Money v={p.paid_toman} /> : <span className="muted">—</span>}</td>
                      <td className="num-col">{p.estimated_toman !== null ? <Money v={p.estimated_toman} /> : <span className="muted">—</span>}</td>
                      <td>
                        <GapChip gap={p.gap} />
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <th>{t("total")}</th>
                    <td className="num-col strong">{m.paid_toman !== null ? <Money v={m.paid_toman} /> : <span className="muted">—</span>}</td>
                    <td className="num-col strong">
                      <Estimate m={m} />
                    </td>
                    <td>
                      <GapChip gap={m.gap} />
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>
          ) : (
            <p className="muted">{t("spendNoEntries")}</p>
          )}
          {m.gap || m.by_provider.some((p) => p.gap) ? <p className="small muted">{t("gapHint")}</p> : null}
          {m.snapshot === null && summary.inventory ? <p className="small muted">{t("spendNoEstimate")}</p> : null}
          <details className="small muted">
            <summary>{t("technicalDetails")}</summary>
            <p>{t("spendEstimateHow")}</p>
          </details>
        </div>
      </Card>

      <Card title={t("spendEntriesOf").replace("{month}", monthName(m.month, lang))}>
        {entries.isLoading ? (
          <Loading />
        ) : entries.error ? (
          <ErrorBox error={entries.error} onRetry={() => entries.refetch()} />
        ) : !entries.data?.length ? (
          <p className="muted">{t("spendNoEntries")}</p>
        ) : (
          <RecList label={t("spendEntriesOf").replace("{month}", monthName(m.month, lang))}>
            {entries.data.map((e) => (
              <Rec
                key={e.id}
                tone={e.status === "needs_review" ? "warn" : undefined}
                icon={<i className="swatch" style={{ background: payeeColor(e.provider_key, e.provider_id) }} aria-hidden />}
                title={e.amount_toman !== null ? <Money v={e.amount_toman} /> : <span className="muted">—</span>}
                meta={
                  <>
                    <bdi>{name(e)}</bdi>
                    <span>{t(SOURCE_LABEL[e.source] ?? "spendSrcManual")}</span>
                    {e.paid_on ? <span>{formatDate(e.paid_on, lang)}</span> : null}
                    {e.reference ? (
                      <span>
                        <bdi>{e.reference}</bdi>
                      </span>
                    ) : null}
                    {e.note ? (
                      <span>
                        <bdi>{e.note}</bdi>
                      </span>
                    ) : null}
                  </>
                }
                aside={
                  <>
                    {e.status === "needs_review" ? <StatusChip tone="warn" label={t("spendCheck")} /> : null}
                    {canEdit ? (
                      <Button variant="ghost" onClick={() => onEdit(e)} aria-label={t("spendEditTitle")}>
                        <Icon name="edit" />
                      </Button>
                    ) : null}
                  </>
                }
              />
            ))}
          </RecList>
        )}
      </Card>
    </div>
  );
}

export function SpendPage() {
  const { t, lang } = useI18n();
  const pname = useProviderName();
  const { activeId, hasRole } = useSession();
  const canEdit = hasRole("analyst");
  const qc = useQueryClient();
  const summary = useQuery({ queryKey: ["spend", activeId], queryFn: () => api.spend(activeId) });
  const [selected, setSelected] = useState<string | null>(null);
  const [editing, setEditing] = useState<SpendEntry | null | "new">(null);
  const [importing, setImporting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const fromInvoice = useMutation({
    mutationFn: (file: File) => api.spendFromInvoice(file),
    onSuccess: (e) => {
      qc.invalidateQueries({ queryKey: ["spend"] });
      qc.invalidateQueries({ queryKey: ["spendEntries"] });
      setEditing(e);
    },
  });

  const data = summary.data;
  const shown = useMemo(() => {
    if (!data) return [];
    const first = data.months.findIndex((m) => m.entries > 0 || m.snapshot !== null);
    const from = first < 0 ? data.months.length : first;
    return data.months.slice(Math.max(0, Math.min(from, data.months.length - 6)));
  }, [data]);
  useEffect(() => {
    if (data && !selected) setSelected(data.current_month);
  }, [data, selected]);

  const chart = useMemo(() => {
    if (!data) return null;
    const legend = new Map<string, { key: string; label: string; color: string; order: { key: string; provider_id: string | null } }>();
    const months: ColumnMonth[] = shown.map((m) => {
      const segs = m.by_provider.filter((p) => p.paid_toman !== null && p.paid_toman !== "0").sort(payeeOrder);
      for (const p of segs) {
        const vendor = !p.provider_id;
        const key = vendor ? "__other" : p.key;
        if (!legend.has(key))
          legend.set(key, { key, label: vendor ? t("spendOther") : pname(p.provider_id, lang), color: payeeColor(p.key, p.provider_id), order: vendor ? { key: "~", provider_id: null } : p });
      }
      return {
        key: m.month,
        label: monthName(m.month, lang),
        short: monthShort(m.month, lang),
        total: m.paid_toman,
        estimate: m.estimated_toman,
        segments: segs.map((p) => ({ key: p.key, label: p.provider_id ? pname(p.provider_id, lang) : p.vendor ?? "—", value: p.paid_toman, color: payeeColor(p.key, p.provider_id) })),
      };
    });
    return { months, legend: [...legend.values()].sort((a, b) => payeeOrder(a.order, b.order)) };
  }, [data, shown, lang, pname, t]);

  const actions = canEdit ? (
    <div className="row gap wrap">
      <Button variant="primary" onClick={() => setEditing("new")}>
        <Icon name="plus" /> {t("spendRecord")}
      </Button>
      <Button busy={fromInvoice.isPending} onClick={() => fileRef.current?.click()}>
        <Icon name="upload" /> {t("spendFromInvoice")}
      </Button>
      <input
        ref={fileRef}
        type="file"
        hidden
        accept=".pdf,.csv,application/pdf,text/csv"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) fromInvoice.mutate(f);
          e.target.value = "";
        }}
      />
      <Button variant="ghost" onClick={() => setImporting(true)}>
        <Icon name="table" /> {t("spendImport")}
      </Button>
    </div>
  ) : null;

  const cur = data?.months.find((m) => m.month === data.current_month);
  const prev = data?.months[data.months.length - 2];
  const sel = data?.months.find((m) => m.month === selected) ?? cur;
  const anything = !!data?.months.some((m) => m.entries > 0) || !!data?.to_review.length;

  return (
    <div className="stack">
      <div className="row between wrap gap">
        <div className="stacked">
          <h1>{t("navSpend")}</h1>
          <p className="muted">{t("spendLead")}</p>
        </div>
        {actions}
      </div>
      <ErrorBox error={fromInvoice.error} />

      {summary.isLoading ? (
        <Loading />
      ) : summary.error ? (
        <ErrorBox error={summary.error} onRetry={() => summary.refetch()} />
      ) : data && chart ? (
        <>
          {data.to_review.length ? (
            <Card title={t("spendReviewTitle")} className="card-warn">
              <p className="muted">{t("spendReviewBody")}</p>
              <RecList label={t("spendReviewTitle")}>
                {data.to_review.map((e) => (
                  <Rec
                    key={e.id}
                    tone="warn"
                    icon={<Icon name="file" />}
                    title={e.amount_toman !== null ? <Money v={e.amount_toman} /> : <span className="muted">—</span>}
                    meta={
                      <>
                        <bdi>{e.provider_id ? pname(e.provider_id, lang) : e.vendor ?? "—"}</bdi>
                        <span>{monthName(e.month, lang)}</span>
                        {e.upload ? <bdi>{e.upload.filename}</bdi> : null}
                      </>
                    }
                    aside={
                      canEdit ? (
                        <Button variant="primary" onClick={() => setEditing(e)}>
                          {t("spendCheck")}
                        </Button>
                      ) : null
                    }
                  />
                ))}
              </RecList>
            </Card>
          ) : null}

          {!anything ? (
            <Card>
              <Empty>
                <Icon name="money" />
                <h2>{t("spendNothing")}</h2>
                <p className="muted">{t("spendNothingBody")}</p>
                {actions}
              </Empty>
            </Card>
          ) : (
            <>
              <div className="stats">
                <div className="stat">
                  <span className="stat-label">{t("spendThisMonth")}</span>
                  {cur?.paid_toman ? <Money v={cur.paid_toman} big /> : <span className="money money-big muted">—</span>}
                  {cur ? <ChangeLine m={cur} /> : null}
                </div>
                <div className="stat">
                  <span className="stat-label">{t("spendLastMonth")}</span>
                  {prev?.paid_toman ? <Money v={prev.paid_toman} big /> : <span className="money money-big muted">—</span>}
                  {prev ? <small className="muted">{monthName(prev.month, lang)}</small> : null}
                </div>
                <div className="stat">
                  <span className="stat-label">{t("spendEstimatedThisMonth")}</span>
                  {cur?.estimated_toman ? <Money v={cur.estimated_toman} big /> : <span className="money money-big muted">—</span>}
                  {cur?.gap ? <GapChip gap={cur.gap} /> : null}
                </div>
                <div className="stat">
                  <span className="stat-label">{t("spendYear")}</span>
                  {data.period_paid_toman ? <Money v={data.period_paid_toman} big /> : <span className="money money-big muted">—</span>}
                </div>
              </div>

              <Card title={t("spendTrend")}>
                <MonthColumns months={chart.months} selected={sel?.month ?? null} onSelect={setSelected} caption={t("spendTrendCaption")} estimateLabel={t("spendEstimateKey")} legend={chart.legend} />
              </Card>

              <Card title={t("spendMonthsTitle")}>
                <div className="table-wrap">
                  <table className="table compact">
                    <thead>
                      <tr>
                        <th>{t("spendMonth")}</th>
                        <th className="num-col">{t("spendPaid")}</th>
                        <th className="num-col">{t("spendChange")}</th>
                        <th className="num-col">{t("spendEstimateKey")}</th>
                        <th>{t("spendDiff")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...shown].reverse().map((m) => (
                        <tr key={m.month} className={m.month === sel?.month ? "selected" : undefined}>
                          <td>
                            <button type="button" className="linklike" aria-pressed={m.month === sel?.month} onClick={() => setSelected(m.month)}>
                              {monthName(m.month, lang)}
                            </button>
                            {lang === "en" ? (
                              <small className="muted">
                                {" "}
                                {formatDate(m.starts_on, lang)} – {formatDate(m.ends_on, lang)}
                              </small>
                            ) : null}
                          </td>
                          <td className="num-col">{m.paid_toman !== null ? <Money v={m.paid_toman} /> : <span className="muted">—</span>}</td>
                          <td className="num-col">
                            <Change amount={m.change_toman} />
                          </td>
                          <td className="num-col">
                            <Estimate m={m} />
                          </td>
                          <td>
                            <GapChip gap={m.gap} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            </>
          )}

          {sel && anything ? <MonthDetail key={sel.month} m={sel} summary={data} canEdit={canEdit} onEdit={setEditing} /> : null}
        </>
      ) : null}

      {editing && data ? <EntryDrawer key={editing === "new" ? "new" : editing.id} entry={editing === "new" ? null : editing} month={sel?.month ?? data.current_month} current={data.current_month} onClose={() => setEditing(null)} /> : null}
      {importing ? <ImportDrawer onClose={() => setImporting(false)} /> : null}
    </div>
  );
}

