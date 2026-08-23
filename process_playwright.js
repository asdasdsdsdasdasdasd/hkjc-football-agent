#!/usr/bin/env node
/**
 * Process remaining HKJC matches using Playwright via CDP directly.
 * Key insight: the SPA uses hash-based routing.
 * We need to navigate to #search and keep it there.
 */
const { chromium } = require('playwright');

const OUTPUT = '/mnt/d/openclaw/hkjc-football-agent/output/june-full-records.json';
const TARGET_IDS = ['FB1079','FB1080','FB1084','FB1093','FB1102','FB1103','FB1108','FB1109'];
const BASE = 'https://bet.hkjc.com/ch/football/results';

async function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function clickDetailButton(page, matchId) {
  // Find the match row and click the detail button (first button)
  const row = page.locator('.fb-results-match-results .table-row').filter({ hasText: matchId });
  const detailBtns = row.locator('.detail-cell .detail-btn');
  const count = await detailBtns.count();
  if (count === 0) {
    console.log(`  No detail buttons for ${matchId}`);
    return false;
  }
  // First button = detail/stats
  await detailBtns.nth(0).click();
  return true;
}

async function clickOddsButton(page, matchId) {
  const row = page.locator('.fb-results-match-results .table-row').filter({ hasText: matchId });
  const detailBtns = row.locator('.detail-cell .detail-btn');
  const count = await detailBtns.count();
  if (count < 2) {
    console.log(`  Only ${count} detail buttons for ${matchId}`);
    return false;
  }
  // Second button = odds (%)
  await detailBtns.nth(count - 1).click();
  return true;
}

async function getCorners(page) {
  const text = await page.locator('body').innerText();
  
  const parseCorner = (label) => {
    const re = new RegExp(label + '[\\s\\S]{0,80}?(\\d+)\\s*\\(\\s*(\\d+)\\s*:\\s*(\\d+)\\s*\\)');
    const m = text.match(re);
    if (!m) return null;
    return { total: +m[1], home: +m[2], away: +m[3] };
  };
  
  return {
    half_time_corners: parseCorner('半場角球') || parseCorner('半场角球'),
    full_time_corners: parseCorner('全場角球') || parseCorner('全场角球')
  };
}

async function getOdds(page) {
  const text = await page.locator('body').innerText();
  const WANT = ['主客和','半場主客和','讓球','半場讓球','入球大細','半場入球大細','開出角球大細','半場開出角球大細'];
  const out = {};
  
  for (const name of WANT) {
    const idx = text.indexOf(name);
    if (idx < 0) { out[name] = []; continue; }
    let slice = text.slice(idx, idx + 2500);
    
    // Check for 即場 (live) variant
    if (/即場/.test(slice.slice(0, name.length + 30))) {
      // Try to find a non-即場 section
      const nextIdx = text.indexOf(name, idx + 1);
      if (nextIdx > 0 && !text.slice(nextIdx - 50, nextIdx + name.length + 30).includes('即場')) {
        slice = text.slice(nextIdx, nextIdx + 2500);
      } else {
        out[name] = [];
        continue;
      }
    }
    
    const lines = [];
    
    if (name.includes('主客和')) {
      // 1X2 format: 主隊勝 (...) odds or 主隊勝  odds
      const hda1 = [...slice.matchAll(/(主隊勝|和|客隊勝)\s*[\(（][^\)）]+[\)）]?\s*(\d+\.?\d*)/g)];
      const hda2 = [...slice.matchAll(/(主隊勝|和|客隊勝)[^\d]*?(\d+\.?\d*)/g)];
      const hda = hda1.length ? hda1 : hda2;
      out[name] = hda.map(x => ({ selection: x[1], odds: parseFloat(x[2]) }));
    } else {
      // Line-based: [8.5] 1.64 or -0.5/-1 2.01
      const lineRe = /(\[[^\]]+\]|[-+]?\d+(?:\.\d+)?(?:\/[-+]?\d+(?:\.\d+)?)?)\s+(\d+\.\d+)/g;
      let m;
      while ((m = lineRe.exec(slice)) !== null) {
        // Determine if this is over/under or just a straight line
        const before = slice.slice(Math.max(0, m.index - 30), m.index);
        const isOver = /大|over/i.test(before);
        const isUnder = /細|under/i.test(before);
        lines.push({ line: m[1], odds: parseFloat(m[2]) });
      }
      out[name] = lines;
    }
  }
  return out;
}

async function closePanel(page) {
  // Try pressing Escape
  await page.keyboard.press('Escape');
  await sleep(1000);
  
  // Then try clicking close button
  try {
    const closeBtn = page.locator('[class*="close"], [class*="Close"], .modal-close');
    if (await closeBtn.count() > 0) {
      await closeBtn.first().click();
      await sleep(500);
    }
  } catch(e) {}
}

async function main() {
  console.log('Launching browser via CDP...');
  
  // Connect to existing Chrome instance at port 9222
  const browser = await chromium.connectOverCDP('http://127.0.0.1:9222');
  console.log('Connected!');
  
  const context = browser.contexts()[0];
  
  // Load existing data
  const fs = require('fs');
  const data = JSON.parse(fs.readFileSync(OUTPUT, 'utf8'));
  console.log(`Loaded ${data.matches.length} matches`);
  
  function findMatch(mid) {
    return data.matches.find(m => m.match_id === mid);
  }
  
  function save() {
    data.recorded_at = new Date().toISOString().replace('Z','+08:00');
    data.match_count = data.matches.length;
    fs.writeFileSync(OUTPUT, JSON.stringify(data, null, 2), 'utf8');
  }
  
  // Open a new page in the existing context
  const page = await context.newPage();
  console.log('Created new page');
  
  for (const mid of TARGET_IDS) {
    const match = findMatch(mid);
    if (!match) {
      console.log(`WARNING: ${mid} not found in data`);
      continue;
    }
    
    console.log(`\n=== Processing ${mid}: ${match.teams} ===`);
    
    // Check completeness
    const hasCorners = match.corners?.full_time?.total > 0;
    const hasOdds = match.odds_closing && Object.values(match.odds_closing).some(v => v.length > 0);
    
    if (hasCorners && hasOdds) {
      console.log(`  Already complete, skipping`);
      continue;
    }
    
    if (mid === 'FB1108') {
      // Void match
      match.corners = { half_time: null, full_time: null };
      match.odds_closing = {"主客和":[],"半場主客和":[],"讓球":[],"半場讓球":[],"入球大細":[],"半場入球大細":[],"開出角球大細":[],"半場開出角球大細":[]};
      save();
      console.log(`  Void match, set empty data`);
      continue;
    }
    
    // Navigate to search page
    await page.goto(BASE + '#search', { waitUntil: 'networkidle' });
    await sleep(2000);
    
    // Check if search view shows all matches or compact
    const compactBtn = page.locator('button, div, span').filter({ hasText: '選擇其他賽事' }).first();
    if (await compactBtn.isVisible().catch(() => false)) {
      console.log(`  Search panel is compact (顯示過濾類別), clicking "選擇其他賽事"...`);
      // Actually "選擇其他賽事" is showing filtered matches. Let me check what date is set
    }
    
    // Check current date range
    const dateInput = page.locator('input.date-input');
    const currentDate = await dateInput.inputValue().catch(() => '');
    console.log(`  Current date: ${currentDate}`);
    
    // Navigate to search before filtering to match 28/06
    // Actually all our targets are on 28/06 so let me just click 賽果 tab
    await page.goto(BASE + '#search', { waitUntil: 'networkidle' });
    await sleep(2000);
    
    // The page might need date set to 28/06 
    // Let me check what matches are visible
    const visibleText = await page.locator('.fb-results-match-results').innerText().catch(() => 'empty');
    
    // Check if our match is visible
    if (!visibleText.includes(mid)) {
      console.log(`  Match ${mid} not visible in current view, searching...`);
      // Set date to 28/06/2026
      await dateInput.click();
      await dateInput.fill('');
      await dateInput.type('28/06/2026 - 28/06/2026', { delay: 50 });
      await sleep(500);
      
      // Click search button
      const searchBtn = page.getByRole('button', { name: '搜尋' });
      if (await searchBtn.isVisible()) {
        await searchBtn.click();
      } else {
        // Try clicking the compact search toggle
        const toggle = page.locator('button, div').filter({ hasText: '搜尋' }).first();
        await toggle.click().catch(() => {});
      }
      await sleep(3000);
    }
    
    // Step 1: Click detail button for corners
    const row = page.locator('.fb-results-match-results .table-row').filter({ hasText: mid });
    const detailBtns = row.locator('.detail-cell > div');
    const btnCount = await detailBtns.count();
    console.log(`  Found ${btnCount} detail buttons`);
    
    if (btnCount > 0 && !hasCorners) {
      console.log(`  Clicking detail button (btn 1 of ${btnCount})...`);
      await detailBtns.nth(0).click({ force: true });
      await sleep(3000);
      
      // Extract corners
      const corners = await getCorners(page);
      console.log(`  Corners:`, JSON.stringify(corners));
      match.corners = {
        half_time: corners.half_time_corners || null,
        full_time: corners.full_time_corners || null
      };
      
      // Close panel
      await closePanel(page);
      await sleep(2000);
    }
    
    // Step 2: Click odds button (%) 
    if (btnCount > 1 && !hasOdds) {
      console.log(`  Clicking odds button (btn ${btnCount} of ${btnCount})...`);
      await detailBtns.nth(btnCount - 1).click({ force: true });
      await sleep(3000);
      
      // Extract odds
      const odds = await getOdds(page);
      console.log(`  Odds markets found:`);
      for (const [k, v] of Object.entries(odds)) {
        console.log(`    ${k}: ${v.length} items`);
      }
      match.odds_closing = odds;
      
      // Close panel
      await closePanel(page);
      await sleep(2000);
    }
    
    save();
    console.log(`  Saved ${mid}`);
  }
  
  await page.close();
  await browser.close();
  console.log('\n=== ALL DONE ===');
}

main().catch(err => {
  console.error('FATAL:', err);
  process.exit(1);
});
