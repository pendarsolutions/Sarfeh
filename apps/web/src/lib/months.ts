/**
 * Solar Hijri months, as the spend ledger keys them: "1405-08" is Aban 1405. The API sends each month's first and last
 * Gregorian day with it, so nothing here converts calendars.
 */
import { toPersianDigits } from "./money";
import type { Lang } from "./i18n";

const FA = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"];
const EN = ["Farvardin", "Ordibehesht", "Khordad", "Tir", "Mordad", "Shahrivar", "Mehr", "Aban", "Azar", "Dey", "Bahman", "Esfand"];

function split(key: string): [number, number] {
  const [y, m] = key.split("-").map(Number);
  return [y, m];
}

/** «آبان ۱۴۰۵» / "Aban 1405" */
export function monthName(key: string, lang: Lang): string {
  const [y, m] = split(key);
  return lang === "fa" ? `${FA[m - 1]} ${toPersianDigits(String(y))}` : `${EN[m - 1]} ${y}`;
}

/** «آبان» / "Aban", under a chart column */
export function monthShort(key: string, lang: Lang): string {
  const [, m] = split(key);
  return (lang === "fa" ? FA : EN)[m - 1];
}

/** The `count` months ending with `last`, newest first: the choices in a month picker. */
export function recentMonths(last: string, count: number): string[] {
  const [y, m] = split(last);
  const out: string[] = [];
  for (let i = 0; i < count; i++) {
    const idx = y * 12 + (m - 1) - i;
    out.push(`${Math.floor(idx / 12)}-${String((idx % 12) + 1).padStart(2, "0")}`);
  }
  return out;
}
