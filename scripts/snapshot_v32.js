#!/usr/bin/env node
/**
 * Fast v3.2 snapshot: HAD + HT/FT goals (+ corners when weekday).
 * One details call per match. Saturday/Sunday skip corners (not live).
 */
import { FootballAPI } from "hkjc-api";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const DATE = process.argv.includes("--date")
  ? process.argv[process.argv.indexOf("--date") + 1]
  : new Date().toISOString().slice(0, 10);
const OUT = process.argv.includes("--out")
  ? process.argv[process.argv.indexOf("--out") + 1]
  : `output/odds_snapshots/hkjc-api-ch-all-${DATE}.json`;

const weekday = new Date(`${DATE}T12:00:00+08:00`).getDay(); // 0 Sun .. 6 Sat
const isWeekend = weekday === 0 || weekday === 6;
const ODDS_TYPES = isWeekend
  ? ["HAD", "FHA", "HIL", "FHL"]
  : ["HAD", "FHA", "HIL", "FHL", "CHL"];

const SECTION_BY_TYPE = {
  HAD: "主客和",
  FHA: "半場主客和",
  HIL: "入球大細",
  FHL: "半場入球大細",
  CHL: "開出角球大細",
};

function oddNum(x) {
  const n = Number.parseFloat(x);
  return Number.isFinite(n) ? n : null;
}

function lineLabel(condition) {
  return `[${String(condition).replace(/\.0\b/g, "")}]`;
}

function toTeams(match) {
  const home = match.homeTeam?.name_ch || match.homeTeam?.name_en || "?";
  const away = match.awayTeam?.name_ch || match.awayTeam?.name_en || "?";
  return { home, away, teams: `${home} 對 ${away}` };
}

function to1x2(pool, home, away) {
  const line = pool?.lines?.find((l) => l.status === "AVAILABLE") || pool?.lines?.[0];
  const out = [];
  for (const c of line?.combinations || []) {
    if (c.status !== "AVAILABLE") continue;
    const odds = oddNum(c.currentOdds);
    if (!odds) continue;
    if (c.str === "H") out.push({ selection: `${home} (主隊勝)`, odds });
    if (c.str === "A") out.push({ selection: `${away} (客隊勝)`, odds });
    if (c.str === "D") out.push({ selection: "和", odds });
  }
  return out;
}

function toOverUnder(pool) {
  const out = [];
  for (const line of pool?.lines || []) {
    if (line.status !== "AVAILABLE") continue;
    let over = null;
    let under = null;
    for (const c of line.combinations || []) {
      if (c.status !== "AVAILABLE") continue;
      if (c.str === "H") over = oddNum(c.currentOdds);
      if (c.str === "L") under = oddNum(c.currentOdds);
    }
    if (over && under) out.push({ line: lineLabel(line.condition), over_odds: over, under_odds: under });
  }
  return out;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function detailsWithRetry(api, id) {
  for (let attempt = 1; attempt <= 4; attempt++) {
    const match = await api.getFootballMatchDetails(id, ODDS_TYPES);
    if (match) return match;
    await sleep(800 * attempt);
  }
  return null;
}

async function main() {
  const api = new FootballAPI();
  const matches = await api.getAllFootballMatches({
    oddsTypes: ["HAD"],
    showAllMatch: true,
    startIndex: 0,
    endIndex: 300,
  });
  const selected = matches.filter((m) => (m.matchDate || "").slice(0, 10) === DATE);
  console.error(`${DATE} weekday=${weekday} weekend=${isWeekend} types=${ODDS_TYPES.join(",")} matches=${selected.length}`);

  const records = [];
  for (let i = 0; i < selected.length; i++) {
    const match = selected[i];
    if (i > 0 && i % 15 === 0) {
      console.error(`  ${i}/${selected.length}`);
      await sleep(400);
    }
    const { home, away, teams } = toTeams(match);
    const detailed = await detailsWithRetry(api, match.id);
    const pools = Object.fromEntries(
      (detailed?.foPools || []).filter((p) => p?.oddsType).map((p) => [p.oddsType, p])
    );
    const odds_closing = {};
    for (const [type, pool] of Object.entries(pools)) {
      if (type === "HAD" || type === "FHA") {
        const rows = to1x2(pool, home, away);
        if (rows.length) odds_closing[SECTION_BY_TYPE[type]] = rows;
      } else if (SECTION_BY_TYPE[type]) {
        const rows = toOverUnder(pool);
        if (rows.length) odds_closing[SECTION_BY_TYPE[type]] = rows;
      }
    }
    records.push({
      date: `${match.matchDate.slice(8, 10)}/${match.matchDate.slice(5, 7)}/${match.matchDate.slice(0, 4)}`,
      match_id: match.frontEndId || match.id,
      competition: match.tournament?.name_ch || match.tournament?.name_en || "",
      teams,
      kick_off: match.kickOffTime,
      source_id: match.id,
      odds_closing,
    });
  }

  const payload = {
    snapshot_at: new Date().toISOString(),
    source: "hkjc-api",
    scope: "all-v32",
    date_range: DATE,
    odds_types: ODDS_TYPES,
    match_count: records.length,
    matches: records,
  };
  const outPath = resolve(OUT);
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, JSON.stringify(payload, null, 2) + "\n", "utf8");
  const withFhl = records.filter((r) => r.odds_closing["半場入球大細"]?.length).length;
  console.error(`Wrote ${outPath} matches=${records.length} with_FHL=${withFhl}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
