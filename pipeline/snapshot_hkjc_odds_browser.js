#!/usr/bin/env node
/**
 * Browser-based HKJC football odds snapshot.
 *
 * This intentionally uses Playwright and the visible HKJC football pages, not
 * hkjc-api. It captures representative/main lines shown in each market table.
 */

import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const VIEWPORT = { width: 1800, height: 1600 };
const MARKETS = [
  { key: "HAD", labelCh: "主客和", labelEn: "Home/Away/Draw", section: "主客和", kind: "1x2" },
  { key: "FHA", labelCh: "半場主客和", labelEn: "First Half HAD", section: "半場主客和", kind: "1x2" },
  { key: "HIL", labelCh: "入球大細", labelEn: "HiLo", section: "入球大細", kind: "ou" },
  { key: "FHL", labelCh: "半場入球大細", labelEn: "First Half HiLo", section: "半場入球大細", kind: "ou" },
  { key: "HLH", labelCh: "球隊入球大細", labelEn: "Team HiLo", section: "球隊入球大細", kind: "team_ou" },
  { key: "FLH", labelCh: "球隊半場入球大細", labelEn: "First Half Team HiLo", section: "球隊半場入球大細", kind: "team_ou" },
  { key: "CHL", labelCh: "開出角球大細", labelEn: "Corner Taken HiLo", section: "開出角球大細", kind: "ou" },
  { key: "FCH", labelCh: "半場開出角球大細", labelEn: "First Half Corner Taken HiLo", section: "半場開出角球大細", kind: "ou" },
  { key: "CHH", labelCh: "球隊開出角球大細", labelEn: "Team Corner Taken HiLo", section: "球隊開出角球大細", kind: "team_ou" },
  { key: "CFH", labelCh: "球隊半場開出角球大細", labelEn: "First Half Team Corner Taken HiLo", section: "球隊半場開出角球大細", kind: "team_ou" },
];

function parseArgs(argv) {
  const args = {
    date: new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Hong_Kong" }),
    days: 3,
    out: null,
    stdout: false,
    headed: false,
    lang: "ch",
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--date") args.date = argv[++i];
    else if (a === "--days") args.days = Number.parseInt(argv[++i], 10);
    else if (a === "--out") args.out = argv[++i];
    else if (a === "--lang") args.lang = argv[++i];
    else if (a === "--stdout") args.stdout = true;
    else if (a === "--headed") args.headed = true;
    else if (a === "--all") {
      // Browser page already shows all competitions.
    } else if (a === "--help" || a === "-h") {
      console.log("Usage: snapshot_hkjc_odds_browser.js [--date YYYY-MM-DD] [--days N] [--lang ch|en] [--out PATH] [--stdout] [--headed]");
      process.exit(0);
    } else {
      throw new Error(`Unknown option: ${a}`);
    }
  }
  if (!Number.isFinite(args.days) || args.days < 1) throw new Error("--days must be a positive integer");
  if (!["ch", "en"].includes(args.lang)) throw new Error("--lang must be ch or en");
  return args;
}

function addDays(iso, days) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function dmyToIso(dmy) {
  return `${dmy.slice(6, 10)}-${dmy.slice(3, 5)}-${dmy.slice(0, 2)}`;
}

function ensureMatch(matches, row) {
  let match = matches.get(row.matchId);
  if (!match) {
    const date = row.dateTime.slice(0, 10);
    match = {
      date,
      match_id: row.matchId,
      competition: row.competition || "",
      teams: `${row.home} 對 ${row.away}`,
      kick_off: `${dmyToIso(date)}T${row.dateTime.slice(11)}:00+08:00`,
      odds_closing: {},
    };
    matches.set(row.matchId, match);
  } else if (!match.competition && row.competition) {
    match.competition = row.competition;
  }
  return match;
}

function normalizeOuLine(raw) {
  if (!raw) return "";
  const text = String(raw).trim().replace(/\.0(?=[/\]])/g, "");
  const m = text.match(/\[[^\]]+\]/);
  return m ? m[0] : text;
}

function applyMarketRows(matches, rows, market, startIso, endIso) {
  for (const row of rows) {
    const iso = dmyToIso(row.dateTime.slice(0, 10));
    if (iso < startIso || iso > endIso) continue;
    const match = ensureMatch(matches, row);

    if (market.kind === "1x2") {
      const [h, d, a] = row.odds || [];
      if ([h, d, a].every((n) => Number.isFinite(n) && n > 1)) {
        match.odds_closing[market.section] = [
          { selection: `${row.home} (主隊勝)`, odds: h },
          { selection: "和", odds: d },
          { selection: `${row.away} (客隊勝)`, odds: a },
        ];
      }
      continue;
    }

    if (market.kind === "team_ou") {
      const entries = row.teamLines || [];
      if (entries.length) {
        match.odds_closing[market.section] = entries.map((t) => ({
          team: t.team,
          line: normalizeOuLine(t.line),
          over_odds: t.over,
          under_odds: t.under,
        }));
      }
      continue;
    }

    const [over, under] = row.odds || [];
    if (row.line && Number.isFinite(over) && Number.isFinite(under) && over > 1 && under > 1) {
      match.odds_closing[market.section] = [{ line: normalizeOuLine(row.line), over_odds: over, under_odds: under }];
    }
  }
}

async function extractMarketRows(page, marketKind) {
  return page.evaluate((kind) => {
    const rows = [];
    for (const row of document.querySelectorAll(".fbOddsTable .match-row")) {
      const dateTime = row.querySelector(".date")?.innerText?.trim() || "";
      const matchId = row.querySelector(".fb-id")?.innerText?.trim() || "";
      if (!/^\d{2}\/\d{2}\/\d{4} \d{2}:\d{2}$/.test(dateTime)) continue;
      if (!/^FB\d+$/.test(matchId)) continue;

      const img = row.querySelector(".tourn img");
      const competition = img?.getAttribute("alt") || img?.title || "";
      const home = row.querySelector('[data-testid$="_homeTeam"]')?.innerText?.trim() || "";
      const away = row.querySelector('[data-testid$="_awayTeam"]')?.innerText?.trim() || "";
      if (!home || !away) continue;

      let line = "";
      let odds = [];
      let teamLines = [];
      if (kind === "1x2") {
        odds = (row.querySelector(".odds")?.innerText || "")
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean)
          .map((s) => Number.parseFloat(s))
          .filter((n) => Number.isFinite(n) && n > 1 && n < 100)
          .slice(0, 3);
      } else if (kind === "team_ou") {
        const oddsLines = row.querySelectorAll(".oddsLine");
        oddsLines.forEach((oddsLine, idx) => {
          const lineEl = oddsLine.querySelector(".lineNum.show") || oddsLine.querySelector(".lineNum");
          const teamLineRaw = (lineEl?.innerText || "").trim();
          const teamLineMatch = teamLineRaw.match(/\[[^\]]+\]/);
          const teamLine = teamLineMatch ? teamLineMatch[0].replace(/\.0(?=[/\]])/g, "") : teamLineRaw.replace(/\.0(?=[/\]])/g, "");
          const teamOdds = (oddsLine.innerText || "")
            .split("\n")
            .map((s) => s.trim())
            .filter(Boolean)
            .map((s) => Number.parseFloat(s))
            .filter((n) => Number.isFinite(n) && n > 1 && n < 100)
            .slice(0, 2);
          if (teamLine && teamOdds.length >= 2) {
            teamLines.push({ team: idx === 0 ? "home" : "away", line: teamLine, over: teamOdds[0], under: teamOdds[1] });
          }
        });
      } else {
        const oddsLine = row.querySelector(".oddsLine");
        const lineEl = oddsLine?.querySelector(".lineNum.show") || oddsLine?.querySelector(".lineNum");
        line = (lineEl?.innerText || "").trim().replace(/\.0(?=[/\]])/g, "");
        odds = (oddsLine?.innerText || "")
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean)
          .map((s) => Number.parseFloat(s))
          .filter((n) => Number.isFinite(n) && n > 1 && n < 100)
          .slice(0, 2);
      }

      rows.push({ dateTime, matchId, competition, home, away, line, odds, teamLines });
    }
    return rows;
  }, marketKind);
}

async function clickMarket(page, label) {
  const ok = await page.evaluate((label) => {
    const roots = [...document.querySelectorAll(".leftMenuMain *,.fbMain *")];
    const el = roots.find((e) => (e.innerText || "").trim() === label);
    if (!el) return false;
    el.click();
    return true;
  }, label);
  if (!ok) throw new Error(`Market tab not found: ${label}`);
  await page.waitForTimeout(3500);
}

async function main() {
  const args = parseArgs(process.argv);
  const endDate = addDays(args.date, args.days - 1);
  const matches = new Map();
  const browser = await chromium.launch({ headless: !args.headed });
  const context = await browser.newContext({ locale: args.lang === "en" ? "en-HK" : "zh-HK", viewport: VIEWPORT });
  const page = await context.newPage();
  await page.goto(`https://bet.hkjc.com/${args.lang}/football`, { waitUntil: "domcontentloaded", timeout: 90000 });
  await page.waitForTimeout(8000);

  for (const market of MARKETS) {
    await clickMarket(page, args.lang === "en" ? market.labelEn : market.labelCh);
    const rows = await extractMarketRows(page, market.kind);
    applyMarketRows(matches, rows, market, args.date, endDate);
  }

  await browser.close();
  const records = [...matches.values()].sort((a, b) => `${a.date} ${a.kick_off} ${a.match_id}`.localeCompare(`${b.date} ${b.kick_off} ${b.match_id}`));
  const payload = {
    snapshot_at: new Date().toISOString(),
    source: "hkjc-browser",
    language: args.lang,
    scope: "all",
    date_range: args.days === 1 ? args.date : `${args.date}..${endDate}`,
    match_count: records.length,
    markets: MARKETS.map((m) => m.section),
    matches: records,
  };

  const json = JSON.stringify(payload, null, 2);
  if (args.stdout) console.log(json);
  if (args.out) {
    const outPath = resolve(args.out);
    mkdirSync(dirname(outPath), { recursive: true });
    writeFileSync(outPath, json + "\n", "utf8");
    console.error(`Wrote ${outPath}`);
  } else if (!args.stdout) {
    const stamp = payload.snapshot_at.replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
    const outPath = resolve("output", "odds_snapshots", `hkjc-browser-${args.lang}-all-${args.date}-${stamp}.json`);
    mkdirSync(dirname(outPath), { recursive: true });
    writeFileSync(outPath, json + "\n", "utf8");
    console.error(`Wrote ${outPath}`);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
