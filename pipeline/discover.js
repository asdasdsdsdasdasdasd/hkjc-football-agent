#!/usr/bin/env node
/**
 * Discover HKJC football matches via GraphQL (hkjc-api) and insert into SQLite.
 *
 * Usage:
 *   node pipeline/discover.js --start 2026-06-01 --end 2026-06-30
 *   node pipeline/discover.js --start 2026-06-01 --end 2026-06-30 --db data/pipeline.db
 */

import Database from "better-sqlite3";
import { FootballAPI } from "hkjc-api";
import { mkdirSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..");

const SCHEMA = `
CREATE TABLE IF NOT EXISTS matches (
  match_id TEXT PRIMARY KEY,
  match_date TEXT NOT NULL,
  front_end_id TEXT,
  api_id TEXT,
  competition TEXT,
  teams TEXT,
  ht_score TEXT,
  ft_score TEXT,
  status TEXT DEFAULT 'pending',
  retries INTEGER DEFAULT 0,
  last_error TEXT,
  scraped_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON matches(status);
CREATE INDEX IF NOT EXISTS idx_date ON matches(match_date);
`;

function parseArgs(argv) {
  const args = { start: null, end: null, db: resolve(ROOT, "data/pipeline.db") };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--start") args.start = argv[++i];
    else if (a === "--end") args.end = argv[++i];
    else if (a === "--db") args.db = resolve(argv[++i]);
    else if (a === "--help" || a === "-h") {
      console.log("Usage: node pipeline/discover.js --start YYYY-MM-DD --end YYYY-MM-DD [--db path]");
      process.exit(0);
    }
  }
  if (!args.start || !args.end) {
    console.error("Required: --start YYYY-MM-DD --end YYYY-MM-DD");
    process.exit(1);
  }
  return args;
}

function* dateRange(startIso, endIso) {
  const cur = new Date(startIso + "T00:00:00Z");
  const end = new Date(endIso + "T00:00:00Z");
  while (cur <= end) {
    yield cur.toISOString().slice(0, 10);
    cur.setUTCDate(cur.getUTCDate() + 1);
  }
}

function toTeams(home, away) {
  const h = home?.name_ch || home?.name_en || "?";
  const a = away?.name_ch || away?.name_en || "?";
  return `${h} 對 ${a}`;
}

function extractScores(results) {
  let ht = null;
  let ft = null;
  if (!Array.isArray(results)) return { ht, ft };
  for (const r of results) {
    const stage = r?.stageId;
    const home = r?.homeResult;
    const away = r?.awayResult;
    if (home == null || away == null) continue;
    const score = `${home} : ${away}`;
    if (stage === 3) ht = score;
    if (stage === 2) ft = score;
  }
  return { ht, ft };
}

async function fetchDay(api, dateIso) {
  const matches = [];
  const seen = new Set();
  let startIndex = 0;
  const pageSize = 20;
  const maxPages = 50;

  for (let page = 0; page < maxPages; page++) {
    const resp = await api.searchHistoricFootballMatches({
      startDate: dateIso,
      endDate: dateIso,
      startIndex,
      endIndex: startIndex + pageSize - 1,
    });
    const batch = resp?.matches ?? [];
    if (!batch.length) break;

    let added = 0;
    for (const m of batch) {
      const id = m.frontEndId || m.matchId;
      if (!id || seen.has(id)) continue;
      seen.add(id);
      matches.push(m);
      added++;
    }
    if (added === 0) break;

    const total = resp?.matchNumByDate?.total;
    if (total != null && matches.length >= total) break;
    startIndex += pageSize;
  }
  return matches;
}

function openDb(dbPath) {
  mkdirSync(dirname(dbPath), { recursive: true });
  const db = new Database(dbPath);
  db.exec(SCHEMA);
  return db;
}

const upsertStmt = (db) =>
  db.prepare(`
    INSERT INTO matches (
      match_id, match_date, front_end_id, api_id, competition, teams,
      ht_score, ft_score, status
    ) VALUES (
      @match_id, @match_date, @front_end_id, @api_id, @competition, @teams,
      @ht_score, @ft_score, 'pending'
    )
    ON CONFLICT(match_id) DO UPDATE SET
      front_end_id=COALESCE(excluded.front_end_id, matches.front_end_id),
      api_id=COALESCE(excluded.api_id, matches.api_id),
      competition=COALESCE(excluded.competition, matches.competition),
      teams=COALESCE(excluded.teams, matches.teams),
      ht_score=COALESCE(excluded.ht_score, matches.ht_score),
      ft_score=COALESCE(excluded.ft_score, matches.ft_score)
  `);

async function main() {
  const args = parseArgs(process.argv);
  const db = openDb(args.db);
  const insert = upsertStmt(db);
  const api = new FootballAPI();

  let total = 0;
  for (const day of dateRange(args.start, args.end)) {
    process.stdout.write(`Discover ${day}... `);
    try {
      const batch = await fetchDay(api, day);
      for (const m of batch) {
        const matchId = m.frontEndId || m.matchId;
        if (!matchId) continue;
        const apiDate = m.matchDate?.slice(0, 10);
        const { ht, ft } = extractScores(m.results);
        insert.run({
          match_id: matchId,
          match_date: apiDate || day,
          front_end_id: m.frontEndId || matchId,
          api_id: m.id != null ? String(m.id) : null,
          competition: m.tournament?.name_ch || m.tournament?.name_en || null,
          teams: toTeams(m.homeTeam, m.awayTeam),
          ht_score: ht,
          ft_score: ft,
        });
        total++;
      }
      console.log(`${batch.length} matches`);
    } catch (err) {
      console.log(`ERROR: ${err.message}`);
    }
  }

  const row = db.prepare("SELECT COUNT(*) AS n FROM matches").get();
  console.log(`\nInserted/updated ${total} rows this run. DB total: ${row.n}`);
  db.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
