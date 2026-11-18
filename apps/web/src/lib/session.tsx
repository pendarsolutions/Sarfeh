import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, loadSession, saveSession, UNAUTHORIZED_EVENT } from "../api/client";
import type { InventorySummary, Role, Session } from "../api/types";

const RANK: Role[] = ["viewer", "analyst", "admin", "owner"];

interface SessionCtx {
  session: Session | null;
  signIn: (s: Session) => void;
  signOut: () => void;
  hasRole: (min: Role) => boolean;
  /** Whether this is a public read-only demo, which signs its visitors in by itself. Undefined until the API says. */
  demo: boolean | undefined;
  /** Why a visitor's session couldn't be started, when it couldn't. */
  demoError: unknown;
  retryDemo: () => void;
  inventories: InventorySummary[] | undefined;
  inventoriesLoading: boolean;
  activeId: string | null;
  setActiveId: (id: string | null) => void;
}

const Ctx = createContext<SessionCtx | null>(null);
const ACTIVE_KEY = "finops.activeInventory";

function readActive(): string | null {
  try {
    return sessionStorage.getItem(ACTIVE_KEY);
  } catch {
    return null;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [session, setSession] = useState<Session | null>(loadSession);
  const [activeId, setActive] = useState<string | null>(readActive);

  const signIn = useCallback(
    (s: Session) => {
      saveSession(s);
      qc.clear();
      setSession(s);
    },
    [qc],
  );
  const signOut = useCallback(() => {
    saveSession(null);
    qc.clear();
    setSession(null);
  }, [qc]);

  useEffect(() => {
    const h = () => setSession(null);
    window.addEventListener(UNAUTHORIZED_EVENT, h);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, h);
  }, []);

  // A public demo has no sign-in. Once the API says this is one, a visitor's session is asked for, and asked for
  // again whenever it ends. The answer is kept in state: signing in clears every cached request, this one too.
  const [demo, setDemo] = useState<boolean | undefined>(undefined);
  const [demoError, setDemoError] = useState<unknown>(null);
  const asking = useRef(false);
  const status = useQuery({ queryKey: ["authStatus"], queryFn: api.authStatus });
  useEffect(() => {
    if (status.data) setDemo(status.data.demo === true);
    else if (status.isError) setDemo(false);
  }, [status.data, status.isError]);
  useEffect(() => {
    if (!demo || session || demoError || asking.current) return;
    asking.current = true;
    api
      .demoSession()
      .then(signIn)
      .catch((e: unknown) => setDemoError(e))
      .finally(() => {
        asking.current = false;
      });
  }, [demo, session, demoError, signIn]);
  const retryDemo = useCallback(() => setDemoError(null), []);

  const setActiveId = useCallback((id: string | null) => {
    setActive(id);
    try {
      if (id) sessionStorage.setItem(ACTIVE_KEY, id);
      else sessionStorage.removeItem(ACTIVE_KEY);
    } catch {
      /* ignore */
    }
  }, []);

  const invQ = useQuery({ queryKey: ["inventories"], queryFn: api.inventories, enabled: !!session });

  // Keep the active inventory valid: default to the newest one.
  useEffect(() => {
    const list = invQ.data;
    if (!list) return;
    if (!activeId || !list.some((i) => i.id === activeId)) setActiveId(list[0]?.id ?? null);
  }, [invQ.data, activeId, setActiveId]);

  const value = useMemo<SessionCtx>(
    () => ({
      session,
      signIn,
      signOut,
      hasRole: (min) => !!session && RANK.indexOf(session.role) >= RANK.indexOf(min),
      demo,
      demoError,
      retryDemo,
      inventories: invQ.data,
      inventoriesLoading: invQ.isLoading,
      activeId: invQ.data?.some((i) => i.id === activeId) ? activeId : null,
      setActiveId,
    }),
    [session, signIn, signOut, demo, demoError, retryDemo, invQ.data, invQ.isLoading, activeId, setActiveId],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): SessionCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("SessionProvider missing");
  return c;
}
