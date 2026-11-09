import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { InventoryView } from "../api/types";

export function useInventory(id: string | null) {
  return useQuery({ queryKey: ["inventory", id], queryFn: () => api.inventory(id!), enabled: !!id });
}

export function useProviders() {
  return useQuery({ queryKey: ["providers"], queryFn: api.providers, staleTime: 60_000 });
}

/** Mutation returning an InventoryView: refresh caches that depend on the inventory. */
export function useInventoryMutation<A>(fn: (a: A) => Promise<InventoryView>, onDone?: (v: InventoryView) => void) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (v) => {
      qc.setQueryData(["inventory", v.id], v);
      qc.invalidateQueries({ queryKey: ["inventories"] });
      qc.invalidateQueries({ queryKey: ["overview", v.id] });
      onDone?.(v);
    },
  });
}

/** Delete an inventory, then let the session move on to the next one (or the empty workspace). */
export function useDeleteInventory() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteInventory(id),
    onSuccess: (_r, id) => {
      qc.removeQueries({ queryKey: ["inventory", id] });
      qc.removeQueries({ queryKey: ["overview", id] });
      qc.invalidateQueries({ queryKey: ["inventories"] });
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });
}

/** Provider display name in the current language. */
export function useProviderName() {
  const { data } = useProviders();
  return (id: string | null | undefined, lang: "fa" | "en") => {
    if (!id) return "—";
    const p = data?.find((x) => x.id === id);
    if (!p) return id;
    return lang === "fa" ? p.name_fa || p.name_en : p.name_en;
  };
}
