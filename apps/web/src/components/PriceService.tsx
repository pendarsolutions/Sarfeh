import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { NavLink, useLocation } from "react-router-dom";
import { api } from "../api/client";
import { useI18n } from "../lib/i18n";
import { useProviders } from "../lib/hooks";
import { useSession } from "../lib/session";
import { Button, Card, Date_, ErrorBox, StatusChip } from "./ui";
import { Icon } from "./Icon";

const KEY = ["priceService"];

function useProviderNames() {
  const { lang } = useI18n();
  const providers = useProviders();
  return (ids: string[]) =>
    ids
      .map((id) => {
        const p = providers.data?.find((x) => x.id === id);
        return p ? (lang === "fa" ? p.name_fa || p.name_en : p.name_en) : id;
      })
      .join(lang === "fa" ? "، " : ", ");
}

/**
 * Sarfeh's price service on the Catalog page: which prices are in use, whether newer ones are out, and (for admins)
 * updating to them. The installation looks every hour; the prices only change here when an admin updates, unless
 * PRICE_AUTO_UPDATE is set.
 */
export function PriceServiceCard() {
  const { t } = useI18n();
  const { hasRole } = useSession();
  const isAdmin = hasRole("admin");
  const qc = useQueryClient();
  const names = useProviderNames();
  const status = useQuery({ queryKey: KEY, queryFn: api.priceService });
  const refresh = () => {
    for (const k of [KEY, ["providers"], ["catalog"], ["syncRuns"], ["integrations"]]) qc.invalidateQueries({ queryKey: k });
  };
  const check = useMutation({ mutationFn: api.checkPrices, onSuccess: (s) => qc.setQueryData(KEY, s) });
  const update = useMutation({ mutationFn: api.updatePrices, onSuccess: refresh });
  if (status.error) return <ErrorBox error={status.error} onRetry={() => status.refetch()} />;
  const s = status.data;
  if (!s) return null;
  if (s.source !== "sarfeh") {
    return (
      <Card title={t("psTitle")}>
        <p className="muted small">{t(s.source === "scrape" ? "psScrape" : "psOwnCatalog")}</p>
      </Card>
    );
  }
  const done = update.data;
  return (
    <Card
      title={t("psTitle")}
      actions={s.update_available ? <StatusChip tone="warn" label={t("psNew")} /> : s.latest ? <StatusChip tone="good" label={t("psUpToDate")} /> : null}
    >
      <div className="stack">
        <p className="muted small">{t(s.auto_update ? "psLeadAuto" : "psLead")}</p>
        <div className="rec-facts">
          {s.applied && (
            <span>
              {t("psInUse")}: <Date_ v={s.applied.generated_at} />
            </span>
          )}
          {s.latest && (
            <span>
              {t("psNewest")}: <Date_ v={s.latest.generated_at} />
            </span>
          )}
          {s.latest?.read_on && (
            <span>
              {t("psRead")}: <Date_ v={s.latest.read_on} />
            </span>
          )}
          <span>
            {t("psLastCheck")}: <Date_ v={s.checked_at} />
          </span>
        </div>
        {s.error && (
          <p className="alert alert-warn small" role="status">
            {t("psCheckFailed")}: <span dir="ltr">{s.error}</span>
          </p>
        )}
        {s.update_available && (
          <div className="alert alert-info" role="status">
            {s.changed?.length ? t("psNewFor").replace("{p}", names(s.changed)) : t("psNew")}
          </div>
        )}
        {done && (
          <p className="alert alert-info small" role="status">
            {t("psUpdated")} {done.changed.length ? t("psChanged").replace("{p}", names(done.changed)) : t("psNoChange")}
          </p>
        )}
        <ErrorBox error={update.error ?? check.error} />
        <div className="row gap wrap">
          {s.update_available && (
            <Button variant="primary" disabled={!isAdmin} title={!isAdmin ? t("adminOnly") : undefined} busy={update.isPending} onClick={() => update.mutate()}>
              <Icon name="refresh" /> {t("psUpdate")}
            </Button>
          )}
          <Button disabled={!isAdmin} title={!isAdmin ? t("adminOnly") : undefined} busy={check.isPending} onClick={() => check.mutate()}>
            {t("psCheck")}
          </Button>
        </div>
        <small className="muted">
          {t("psCommand")} <code dir="ltr">sudo sarfeh prices update</code>
        </small>
      </div>
    </Card>
  );
}

/** For admins, on every page but the Catalog: new prices are waiting for someone to update. */
export function PriceNotice() {
  const { t } = useI18n();
  const { hasRole, demo } = useSession();
  const loc = useLocation();
  const names = useProviderNames();
  const isAdmin = hasRole("admin");
  const status = useQuery({ queryKey: KEY, queryFn: api.priceService, enabled: isAdmin && !demo, refetchInterval: 15 * 60_000 });
  const s = status.data;
  if (!isAdmin || demo || !s?.update_available || loc.pathname === "/catalog") return null;
  return (
    <div className="alert alert-info price-notice" role="status">
      <span>{s.changed?.length ? t("psNewFor").replace("{p}", names(s.changed)) : t("psNew")}</span>{" "}
      <NavLink to="/catalog">{t("psReview")}</NavLink>
    </div>
  );
}
