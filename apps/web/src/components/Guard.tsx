import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { InventoryView } from "../api/types";
import { useI18n } from "../lib/i18n";
import { useInventory } from "../lib/hooks";
import { useSession } from "../lib/session";
import { EmptyWorkspace } from "../pages/Overview";
import { ErrorBox, Loading } from "./ui";

/**
 * Analysis pages need an active inventory that has been confirmed. While someone edits it, they keep showing the
 * version confirmed last (the API prices that one), with a note, until the changes are confirmed.
 */
export function ConfirmedInventory({ children }: { children: (inv: InventoryView) => ReactNode }) {
  const { t } = useI18n();
  const { activeId, inventoriesLoading } = useSession();
  const inv = useInventory(activeId);
  if (inventoriesLoading || inv.isLoading) return <Loading />;
  if (!activeId) return <EmptyWorkspace />;
  if (inv.error) return <ErrorBox error={inv.error} onRetry={() => inv.refetch()} />;
  if (!inv.data) return null;
  if (inv.data.status !== "confirmed" && !inv.data.has_confirmed_version)
    return (
      <div className="alert alert-warn">
        {t("notConfirmed")} <Link to="/infrastructure?tab=review">{t("goReview")}</Link>
      </div>
    );
  return (
    <>
      {inv.data.status !== "confirmed" ? (
        <div className="alert alert-info">
          {t("editingBanner")} <Link to="/infrastructure?tab=review">{t("goReview")}</Link>
        </div>
      ) : null}
      {children(inv.data)}
    </>
  );
}
