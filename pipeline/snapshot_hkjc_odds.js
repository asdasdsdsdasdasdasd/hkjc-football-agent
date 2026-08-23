#!/usr/bin/env node
/**
 * Snapshot HKJC football odds into the match JSON shape used by recommend_bets.py.
 *
 * Example:
 *   node pipeline/snapshot_hkjc_odds.js --profile 50000118 --date 2026-07-04
 *   node pipeline/snapshot_hkjc_odds.js --tournament 50068132 --days 2 --stdout
 *   node pipeline/snapshot_hkjc_odds.js --all --days 3
 */

import { FootballAPI } from "hkjc-api";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const DEFAULT_ODDS_TYPES = ["HAD", "FHA", "HIL", "FHL", "HLH", "HLA", "FLH", "FLA", "CHL", "FCH", "CHH", "CHA", "CFH", "CFA"];
const SECTION_BY_TYPE = {
  HAD: "主客和",
  FHA: "半場主客和",
  HIL: "入球大細",
  FHL: "半場入球大細",
  HLH: "球隊入球大細",
  HLA: "球隊入球大細",
  FLH: "球隊半場入球大細",
  FLA: "球隊半場入球大細",
  CHL: "開出角球大細",
  FCH: "半場開出角球大細",
  CHH: "球隊開出角球大細",
  CHA: "球隊開出角球大細",
  CFH: "球隊半場開出角球大細",
  CFA: "球隊半場開出角球大細",
};
const TEAM_BY_TYPE = {
  HLH: "home",
  HLA: "away",
  FLH: "home",
  FLA: "away",
  CHH: "home",
  CHA: "away",
  CFH: "home",
  CFA: "away",
};

function parseArgs(argv) {
  const args = {
    profile: "50000118",
    tournament: null,
    date: new Date().toISOString().slice(0, 10),
    days: 1,
    out: null,
    stdout: false,
    all: false,
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--profile") args.profile = argv[++i];
    else if (a === "--tournament") args.tournament = argv[++i];
    else if (a === "--date") args.date = argv[++i];
    else if (a === "--days") args.days = Number.parseInt(argv[++i], 10);
    else if (a === "--out") args.out = argv[++i];
    else if (a === "--stdout") args.stdout = true;
    else if (a === "--all") args.all = true;
    else if (a === "--help" || a === "-h") {
      console.log("Usage: snapshot_hkjc_odds.js [--all|--profile ID|--tournament ID] [--date YYYY-MM-DD] [--days N] [--out PATH] [--stdout]");
      process.exit(0);
    } else {
      throw new Error(`Unknown option: ${a}`);
    }
  }
  if (!Number.isFinite(args.days) || args.days < 1) {
    throw new Error("--days must be a positive integer");
  }
  return args;
}

function addDays(iso, days) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

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

function toOverUnder(pool, team = null) {
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
    if (over && under) {
      const row = { line: lineLabel(line.condition), over_odds: over, under_odds: under };
      if (team) row.team = team;
      out.push(row);
    }
  }
  return out;
}

async function findTournamentId(api, profile) {
  const matches = await api.getAllFootballMatches({
    oddsTypes: ["HAD"],
    showAllMatch: true,
    startIndex: 0,
    endIndex: 200,
  });
  const match = matches.find((m) => m.tournament?.nameProfileId === profile);
  return match?.tournament?.id || null;
}

function mergeMatch(base, incoming) {
  return {
    ...base,
    ...incoming,
    foPools: [...(base?.foPools || []), ...(incoming?.foPools || [])],
  };
}

async function fetchMergedMatches(api, { tournamentId, startDate, endDate }) {
  const byId = new Map();
  for (const type of DEFAULT_ODDS_TYPES) {
    try {
      const matches = await api.getAllFootballMatches({
        tournIds: tournamentId ? [tournamentId] : null,
        oddsTypes: [type],
        showAllMatch: true,
        startDate,
        endDate,
        startIndex: 0,
        endIndex: 300,
      });
      for (const match of matches) {
        const id = match.id || match.frontEndId;
        if (!id) continue;
        const current = byId.get(id);
        byId.set(id, current ? mergeMatch(current, match) : match);
      }
    } catch (err) {
      console.error(`skip odds type ${type}: ${err.message || err}`);
    }
  }
  return [...byId.values()];
}

async function detailsByType(api, id) {
  const out = {};
  for (const type of DEFAULT_ODDS_TYPES) {
    try {
      const match = await api.getFootballMatchDetails(id, [type]);
      const pool = (match?.foPools || []).find((p) => p.oddsType === type);
      if (pool) out[type] = pool;
    } catch (err) {
      console.error(`skip details ${id} ${type}: ${err.message || err}`);
    }
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv);
  const api = new FootballAPI();
  const tournamentId = args.all ? null : args.tournament || (await findTournamentId(api, args.profile));
  if (!args.all && !tournamentId) throw new Error(`Tournament not found for profile ${args.profile}`);

  const endDate = addDays(args.date, args.days - 1);
  const matches = args.all
    ? await api.getAllFootballMatches({
        oddsTypes: ["HAD"],
        showAllMatch: true,
        startIndex: 0,
        endIndex: 300,
      })
    : await fetchMergedMatches(api, {
        tournamentId,
        startDate: args.date,
        endDate,
      });

  const selected = matches.filter((m) => {
    const d = (m.matchDate || "").slice(0, 10);
    return d >= args.date && d <= endDate;
  });

  const records = [];
  for (const match of selected) {
    const { home, away, teams } = toTeams(match);
    const pools = args.all
      ? await detailsByType(api, match.id)
      : Object.fromEntries((match.foPools || []).filter((p) => p?.oddsType).map((p) => [p.oddsType, p]));
    const odds_closing = {};
    for (const [type, pool] of Object.entries(pools)) {
      if (type === "HAD" || type === "FHA") {
        const rows = to1x2(pool, home, away);
        if (rows.length) odds_closing[SECTION_BY_TYPE[type]] = rows;
      } else {
        const rows = toOverUnder(pool, TEAM_BY_TYPE[type] || null);
        if (rows.length) {
          const section = SECTION_BY_TYPE[type];
          odds_closing[section] = [...(odds_closing[section] || []), ...rows];
        }
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
    scope: args.all ? "all" : "tournament",
    tournament_id: tournamentId,
    name_profile_id: args.all ? null : args.profile,
    date_range: args.days === 1 ? args.date : `${args.date}..${endDate}`,
    match_count: records.length,
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
    const outPath = resolve("output", "odds_snapshots", `hkjc-${args.date}-${stamp}.json`);
    mkdirSync(dirname(outPath), { recursive: true });
    writeFileSync(outPath, json + "\n", "utf8");
    console.error(`Wrote ${outPath}`);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
