import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

export type Lang = "fa" | "en";
const KEY = "finops-site-lang";

/** ?lang=en in a link wins, then the visitor's last choice, then Persian. */
function initialLang(): Lang {
  const asked = new URLSearchParams(window.location.search).get("lang");
  if (asked === "fa" || asked === "en") return asked;
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "fa" || saved === "en") return saved;
  } catch {
    /* storage can be blocked; Persian then */
  }
  return "fa";
}

const Ctx = createContext<{ lang: Lang; setLang: (l: Lang) => void }>({ lang: "fa", setLang: () => {} });

const HOME_TITLES = ["FinOps agent: هزینه دقیق زیرساخت ابری", "FinOps agent: exact cloud costs for Iranian providers"] as const;

/** `titles` is the page's own tab title, [fa, en]. */
export function LangProvider({ children, titles = HOME_TITLES }: { children: ReactNode; titles?: readonly [string, string] }) {
  const [lang, setLangState] = useState<Lang>(initialLang);
  useEffect(() => {
    const el = document.documentElement;
    el.lang = lang;
    el.dir = lang === "fa" ? "rtl" : "ltr";
    document.title = titles[lang === "fa" ? 0 : 1];
  }, [lang, titles]);
  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    try {
      localStorage.setItem(KEY, l);
    } catch {
      /* ignore */
    }
  }, []);
  return <Ctx.Provider value={{ lang, setLang }}>{children}</Ctx.Provider>;
}

export const useLang = () => useContext(Ctx);
