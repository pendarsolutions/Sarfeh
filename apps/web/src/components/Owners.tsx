/**
 * Owners and budgets: the team picker, who owns each resource, the organization's teams, and monthly budgets.
 */
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { BudgetStatus, InventoryView } from "../api/types";
import { useI18n } from "../lib/i18n";
import { useProviderName } from "../lib/hooks";
import { useSession } from "../lib/session";
import { formatPercent, formatToman } from "../lib/money";
import { monthName } from "../lib/months";
import { Button, Card, Drawer, ErrorBox, Field, Loading, Money, StatusChip } from "./ui";
import { PartMeter } from "./viz";
import { Icon } from "./Icon";

export function useTeams() {
  return useQuery({ queryKey: ["teams"], queryFn: api.teams, staleTime: 30_000 });
}

export function useTeamName() {
  const { t } = useI18n();
  const teams = useTeams();
  return (id: string | null | undefined) => (id ? teams.data?.find((x) => x.id === id)?.name ?? "—" : t("teamNone"));
}

export function TeamSelect({ id, value, onChange, disabled }: { id?: string; value: string | null; onChange: (v: string | null) => void; disabled?: boolean }) {
  const { t } = useI18n();
  const teams = useTeams();
  return (
    <select id={id} value={value ?? ""} disabled={disabled} onChange={(e) => onChange(e.target.value || null)}>
      <option value="">{t("teamNone")}</option>
      {teams.data?.map((x) => (
        <option key={x.id} value={x.id}>
          {x.name}
        </option>
      ))}
    </select>
  );
}

/** Who owns each resource of an inventory. Not a pricing input, so it never asks for the inventory to be confirmed. */
export function OwnersCard({ inv }: { inv: InventoryView }) {
  const { t, lang } = useI18n();
  const pname = useProviderName();
  const { hasRole } = useSession();
  const canEdit = hasRole("analyst");
  const teams = useTeams();
  const qc = useQueryClient();
  const set = useMutation({
    mutationFn: ({ rid, team }: { rid: string; team: string | null }) => api.setResourceTeam(inv.id, rid, team),
    onSuccess: (v) => {
      qc.setQueryData(["inventory", v.id], v);
      qc.invalidateQueries({ queryKey: ["spend"] });
      qc.invalidateQueries({ queryKey: ["budgets"] });
    },
  });
  return (
    <Card title={t("ownersTitle")}>
      <p className="muted small">{t("ownersLead")}</p>
      {teams.data && !teams.data.length ? (
        <p className="small">
          {t("ownersNoTeams")} <Link to="/settings">{t("navSettings")}</Link>
        </p>
      ) : null}
      <div className="table-wrap">
        <table className="table compact">
          <thead>
            <tr>
              <th>{t("ownersResource")}</th>
              <th>{t("spendProvider")}</th>
              <th>{t("team")}</th>
            </tr>
          </thead>
          <tbody>
            {inv.resources.map((r) => (
              <tr key={r.id}>
                <td>
                  <bdi>{r.name}</bdi>
                  {r.environment ? <small className="muted"> · {r.environment}</small> : null}
                </td>
                <td>{pname(r.provider_id, lang)}</td>
                <td>
                  <label className="sr-only" htmlFor={`own-${r.id}`}>
                    {t("team")}: {r.name}
                  </label>
                  <TeamSelect id={`own-${r.id}`} value={r.team_id ?? null} disabled={!canEdit || set.isPending} onChange={(team) => set.mutate({ rid: r.id, team })} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ErrorBox error={set.error} />
    </Card>
  );
}

/** The organization's teams: add, rename, delete (admins). */
export function TeamsCard() {
  const { t } = useI18n();
  const { hasRole } = useSession();
  const isAdmin = hasRole("admin");
  const qc = useQueryClient();
  const teams = useTeams();
  const [name, setName] = useState("");
  const [editing, setEditing] = useState<{ id: string; name: string } | null>(null);
  const done = () => {
    qc.invalidateQueries({ queryKey: ["teams"] });
    qc.invalidateQueries({ queryKey: ["spend"] });
    qc.invalidateQueries({ queryKey: ["budgets"] });
    qc.invalidateQueries({ queryKey: ["inventory"] });
  };
  const add = useMutation({ mutationFn: () => api.createTeam(name.trim()), onSuccess: () => (setName(""), done()) });
  const rename = useMutation({ mutationFn: (x: { id: string; name: string }) => api.renameTeam(x.id, x.name.trim()), onSuccess: () => (setEditing(null), done()) });
  const del = useMutation({ mutationFn: (id: string) => api.deleteTeam(id), onSuccess: done });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (name.trim()) add.mutate();
  };
  return (
    <Card title={t("teamsTitle")}>
      <p className="muted small">{t("teamsLead")}</p>
      {teams.isLoading ? (
        <Loading />
      ) : teams.data?.length ? (
        <ul className="plain stacked">
          {teams.data.map((x) => (
            <li key={x.id} className="row between gap wrap">
              {editing?.id === x.id ? (
                <form
                  className="row gap wrap"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (editing.name.trim()) rename.mutate(editing);
                  }}
                >
                  <label className="sr-only" htmlFor={`team-${x.id}`}>
                    {t("teamName")}
                  </label>
                  <input id={`team-${x.id}`} value={editing.name} maxLength={100} onChange={(e) => setEditing({ id: x.id, name: e.target.value })} autoFocus />
                  <Button type="submit" variant="primary" busy={rename.isPending}>
                    {t("save")}
                  </Button>
                  <Button variant="ghost" onClick={() => setEditing(null)}>
                    {t("cancel")}
                  </Button>
                </form>
              ) : (
                <>
                  <strong>
                    <bdi>{x.name}</bdi>
                  </strong>
                  {isAdmin ? (
                    <span className="row gap">
                      <Button variant="ghost" onClick={() => setEditing({ id: x.id, name: x.name })} aria-label={`${t("teamRename")}: ${x.name}`}>
                        <Icon name="edit" />
                      </Button>
                      <Button variant="ghost" busy={del.isPending && del.variables === x.id} onClick={() => window.confirm(t("teamDeleteConfirm").replace("{name}", x.name)) && del.mutate(x.id)} aria-label={`${t("delete")}: ${x.name}`}>
                        <Icon name="trash" />
                      </Button>
                    </span>
                  ) : null}
                </>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">{t("teamsNone")}</p>
      )}
      {isAdmin ? (
        <form className="row gap wrap" onSubmit={submit}>
          <label className="sr-only" htmlFor="team-new">
            {t("teamName")}
          </label>
          <input id="team-new" placeholder={t("teamName")} value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />
          <Button type="submit" variant="primary" busy={add.isPending} disabled={!name.trim()}>
            <Icon name="plus" /> {t("teamAdd")}
          </Button>
        </form>
      ) : (
        <small className="muted">{t("adminOnly")}</small>
      )}
      <ErrorBox error={add.error ?? rename.error ?? del.error} />
    </Card>
  );
}

/** Which setup the monthly estimates come from, and when a month's change counts as unusual. */
export function EstimateSettingsCard() {
  const { t } = useI18n();
  const { hasRole, inventories } = useSession();
  const isAdmin = hasRole("admin");
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const spend = useQuery({ queryKey: ["spend"], queryFn: () => api.spend() });
  const s = settings.data?.settings ?? {};
  const [pct, setPct] = useState<string | null>(null);
  const [min, setMin] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (b: Record<string, unknown>) => api.putSettings(b),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["spend"] });
      qc.invalidateQueries({ queryKey: ["budgets"] });
    },
  });
  const pctValue = pct ?? String(s.unusual_change_percent ?? "30");
  const minValue = min ?? String(s.unusual_change_min_toman ?? "1000000");
  return (
    <Card title={t("estimateSettingsTitle")}>
      <div className="stack">
        <Field label={t("estimateInventory")} htmlFor="est-inv" hint={t("estimateInventoryHint")}>
          <select
            id="est-inv"
            disabled={!isAdmin || save.isPending}
            value={(s.estimate_inventory_id as string | undefined) ?? ""}
            onChange={(e) => save.mutate({ estimate_inventory_id: e.target.value || null })}
          >
            <option value="">
              {t("estimateInventoryAuto")}
              {!s.estimate_inventory_id && spend.data?.inventory ? ` (${spend.data.inventory.name})` : ""}
            </option>
            {inventories
              ?.filter((i) => i.status !== "archived")
              .map((i) => (
                <option key={i.id} value={i.id}>
                  {i.name}
                </option>
              ))}
          </select>
        </Field>
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate({ unusual_change_percent: Number(pctValue), unusual_change_min_toman: minValue.replace(/\D/g, "") });
          }}
        >
          <p className="small muted">{t("unusualLead")}</p>
          <div className="grid2">
            <Field label={t("unusualPercent")} htmlFor="un-pct">
              <input id="un-pct" inputMode="numeric" dir="ltr" disabled={!isAdmin} value={pctValue} onChange={(e) => setPct(e.target.value)} />
            </Field>
            <Field label={t("unusualMin")} htmlFor="un-min">
              <input id="un-min" inputMode="numeric" dir="ltr" disabled={!isAdmin} value={minValue} onChange={(e) => setMin(e.target.value)} />
            </Field>
          </div>
          {isAdmin ? (
            <div>
              <Button type="submit" variant="primary" busy={save.isPending} disabled={!/^\d+$/.test(pctValue) || !/^\d+$/.test(minValue.replace(/\D/g, "") || "x")}>
                {t("save")}
              </Button>
            </div>
          ) : (
            <small className="muted">{t("adminOnly")}</small>
          )}
        </form>
        <ErrorBox error={save.error} />
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ budgets

function BudgetRow({ b }: { b: BudgetStatus }) {
  const { t, lang } = useI18n();
  const tone = b.level >= 100 ? "bad" : b.level >= 80 || b.projected_over ? "warn" : "good";
  const label = b.level >= 100 ? t("budgetOver") : b.level >= 80 ? t("budget80") : b.projected_over ? t("budgetProjectedOver") : t("budgetOk");
  return (
    <li className="budget">
      <div className="row between gap wrap">
        <strong>
          <bdi>{b.name ?? t("budgetWhole")}</bdi>
        </strong>
        <StatusChip tone={tone} label={label} />
      </div>
      <PartMeter part={b.paid_toman} whole={b.monthly_toman} label={`${b.name ?? t("budgetWhole")}: ${formatPercent(b.used_percent, lang)}`} />
      <div className="row between gap wrap small">
        <span>
          <Money v={b.paid_toman} /> <span className="muted">{t("budgetOf")}</span> <Money v={b.monthly_toman} /> <span className="muted">({formatPercent(b.used_percent, lang)})</span>
        </span>
        <span className="muted" title={t("budgetProjectedHint")}>
          {t("budgetProjected")}: <span className="num">{formatToman(b.projected_toman, lang)}</span>
        </span>
      </div>
    </li>
  );
}

function BudgetDrawer({ budgets, onClose }: { budgets: BudgetStatus[]; onClose: () => void }) {
  const { t } = useI18n();
  const qc = useQueryClient();
  const teams = useTeams();
  const initial = Object.fromEntries(budgets.map((b) => [b.team_id ?? "", b.monthly_toman]));
  const [values, setValues] = useState<Record<string, string>>(initial);
  const save = useMutation({
    mutationFn: async () => {
      for (const key of new Set([...Object.keys(values), ...Object.keys(initial)])) {
        const v = (values[key] ?? "").replace(/[,٬\s]/g, "").replace(/[۰-۹]/g, (d) => String("۰۱۲۳۴۵۶۷۸۹".indexOf(d)));
        if (v === (initial[key] ?? "")) continue;
        await api.putBudget(key || null, v || null);
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["budgets"] });
      onClose();
    },
  });
  const rows = [{ key: "", name: t("budgetWhole") }, ...(teams.data ?? []).map((x) => ({ key: x.id, name: x.name }))];
  return (
    <Drawer open onClose={onClose} title={t("budgetsEdit")}>
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <p className="muted small">{t("budgetsEditHint")}</p>
        {rows.map((r) => (
          <Field key={r.key} label={<bdi>{r.name}</bdi>} htmlFor={`bud-${r.key || "org"}`}>
            <input id={`bud-${r.key || "org"}`} inputMode="numeric" dir="ltr" placeholder={t("budgetNone")} value={values[r.key] ?? ""} onChange={(e) => setValues((v) => ({ ...v, [r.key]: e.target.value }))} />
          </Field>
        ))}
        <ErrorBox error={save.error} />
        <div className="row gap">
          <Button type="submit" variant="primary" busy={save.isPending}>
            {t("save")}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            {t("cancel")}
          </Button>
        </div>
      </form>
    </Drawer>
  );
}

/** This month's budgets: paid so far against each, and the projected month-end. */
export function BudgetsCard() {
  const { t, lang } = useI18n();
  const { hasRole } = useSession();
  const isAdmin = hasRole("admin");
  const status = useQuery({ queryKey: ["budgets"], queryFn: api.budgets });
  const [editing, setEditing] = useState(false);
  if (status.isLoading) return null;
  if (status.error) return <ErrorBox error={status.error} onRetry={() => status.refetch()} />;
  const data = status.data!;
  if (!data.budgets.length && !isAdmin) return null;
  return (
    <Card
      title={`${t("budgetsTitle")}, ${monthName(data.month, lang)}`}
      actions={
        isAdmin ? (
          <Button onClick={() => setEditing(true)}>
            <Icon name="target" /> {data.budgets.length ? t("budgetsEdit") : t("budgetsSet")}
          </Button>
        ) : null
      }
    >
      {data.budgets.length ? (
        <ul className="plain budgets">
          {data.budgets.map((b) => (
            <BudgetRow key={b.id} b={b} />
          ))}
        </ul>
      ) : (
        <p className="muted">{t("budgetsNone")}</p>
      )}
      {data.budgets.length ? <p className="small muted">{t("budgetProjectedHint")}</p> : null}
      {editing ? <BudgetDrawer budgets={data.budgets} onClose={() => setEditing(false)} /> : null}
    </Card>
  );
}
