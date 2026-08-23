#!/usr/bin/env node
/**
 * Direct CDP script to process remaining HKJC matches
 * Uses the browser's existing tab F21FF645493B992B82B02DDDF1A6A2C1
 */
const fs = require('fs');
const path = require('path');
const http = require('http');
const WebSocket = require('/dev/null'); // using fetch-based approach

const OUTPUT = '/mnt/d/openclaw/hkjc-football-agent/output/june-full-records.json';
const TARGET_IDS = ['FB1079','FB1080','FB1084','FB1093','FB1102','FB1103','FB1108','FB1109'];
const TAB_ID = 'F21FF645493B992B82B02DDDF1A6A2C1';
const DEBUG_WS = 'ws://127.0.0.1:9222/devtools/page/' + TAB_ID;

// Simple CDP over WebSocket
let ws;
let msgId = 1;
const pending = {};

function connect() {
  return new Promise((resolve, reject) => {
    const WebSocket = require('ws');
    ws = new WebSocket(DEBUG_WS);
    ws.on('open', resolve);
    ws.on('message', (data) => {
      const msg = JSON.parse(data.toString());
      if (msg.id && pending[msg.id]) {
        pending[msg.id](msg);
        delete pending[msg.id];
      }
    });
    ws.on('error', reject);
  });
}

function send(method, params = {}) {
  return new Promise((resolve) => {
    const id = msgId++;
    pending[id] = resolve;
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true
  });
  if (result.error) throw new Error(`CDP error: ${JSON.stringify(result.error)}`);
  if (result.result?.exceptionDetails) {
    throw new Error(`JS error: ${result.result.exceptionDetails.text}`);
  }
  return result.result?.value;
}

async function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function main() {
  console.log('Connecting to browser...');
  await connect();
  console.log('Connected!');
  
  // Load existing data
  const data = JSON.parse(fs.readFileSync(OUTPUT, 'utf8'));
  console.log(`Loaded ${data.matches.length} matches`);
  
  function findMatch(mid) {
    return data.matches.find(m => m.match_id === mid);
  }
  
  function save() {
    data.recorded_at = new Date().toISOString().replace('Z','+08:00').replace('T',' ').split(' ')[0] + 'T' + 
      new Date().toISOString().split('T')[1].replace('Z','+08:00');
    data.match_count = data.matches.length;
    fs.writeFileSync(OUTPUT, JSON.stringify(data, null, 2), 'utf8');
    console.log(`Saved ${data.matches.length} matches`);
  }
  
  // Navigate to search page
  console.log('Navigating to search page...');
  await send('Page.navigate', { url: 'https://bet.hkjc.com/ch/football/results#search' });
  await sleep(3000);
  
  for (const mid of TARGET_IDS) {
    const match = findMatch(mid);
    if (!match) {
      console.log(`WARNING: ${mid} not found in data, skipping`);
      continue;
    }
    
    console.log(`\n=== Processing ${mid}: ${match.teams} ===`);
    
    // Check if already complete
    const hasCorners = match.corners?.full_time?.total > 0;
    const hasOdds = match.odds_closing && Object.values(match.odds_closing).some(v => v.length > 0);
    
    if (hasCorners && hasOdds) {
      console.log(`${mid} already complete, skipping`);
      continue;
    }
    
    // For FB1108 (void match), just set empty data
    if (mid === 'FB1108') {
      match.corners = { half_time: null, full_time: null };
      match.odds_closing = {
        "主客和": [], "半場主客和": [], "讓球": [], "半場讓球": [],
        "入球大細": [], "半場入球大細": [], "開出角球大細": [], "半場開出角球大細": []
      };
      save();
      console.log(`${mid}: void match, set empty data`);
      continue;
    }
    
    // Step 1: Find and click the detail button (corners/bar chart icon)
    const clickDetailScript = `
      (() => {
        const rows = document.querySelectorAll('.fb-results-match-results .table-row');
        for (const row of rows) {
          if (row.classList.contains('table-header')) continue;
          const cells = row.querySelectorAll('.table-cell');
          if (cells[1]?.innerText?.trim() !== '${mid}') continue;
          
          // Look for the detail buttons - there should be 2 in detail-cell
          const detailCell = row.querySelector('.detail-cell');
          if (!detailCell) return { ok: false, error: 'no detail-cell' };
          
          const buttons = detailCell.querySelectorAll(':scope > div');
          // First button should be the detail/stats button (shown as bar chart icon)
          if (buttons.length > 0) {
            buttons[0].click();
            return { ok: true, action: 'clicked_detail', matchId: '${mid}' };
          }
          return { ok: false, error: 'no buttons found' };
        }
        return { ok: false, error: 'match row not found', matchId: '${mid}' };
      })()
    `;
    
    let result = await evaluate(clickDetailScript);
    console.log(`  Click detail result:`, JSON.stringify(result));
    
    if (result?.ok) {
      await sleep(2000);
      
      // Extract corners from modal/panel
      const cornerScript = `
        (() => {
          const text = document.body.innerText;
          const parseCorner = (label) => {
            const re = new RegExp(label + '[\\\\s\\\\S]{0,80}?(\\\\d+)\\\\s*\\\\((\\\\s*\\\\d+\\\\s*:\\\\s*\\\\d+)\\\\s*\\\\)');
            const m = text.match(re);
            if (!m) return null;
            const parts = m[2].split(':').map(s => parseInt(s.trim()));
            return { total: +m[1], home: parts[0], away: parts[1] };
          };
          const parseCornerAlt = (label) => {
            const re = new RegExp(label + '[\\\\s\\\\S]{0,80}?(\\\\d+)\\\\s*\\\\(\\\\s*(\\\\d+)\\\\s*\\\\:\\\\s*(\\\\d+)\\\\s*\\\\)');
            const m = text.match(re);
            if (!m) return null;
            return { total: +m[1], home: +m[2], away: +m[3] };
          };
          return {
            half_time_corners: parseCorner('半場角球') || parseCornerAlt('半場角球') || parseCorner('半场角球') || parseCornerAlt('半场角球'),
            full_time_corners: parseCorner('全場角球') || parseCornerAlt('全場角球') || parseCorner('全场角球') || parseCornerAlt('全场角球')
          };
        })()
      `;
      
      const corners = await evaluate(cornerScript);
      console.log(`  Corners:`, JSON.stringify(corners));
      
      // Update match record
      match.corners = {
        half_time: corners?.half_time_corners || null,
        full_time: corners?.full_time_corners || null
      };
      
      // Close the detail panel - try Escape
      await evaluate(`document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))`);
      await sleep(1000);
      
      // Also try clicking close
      await evaluate(`
        (() => {
          const closeBtn = document.querySelector('.modal-close') || document.querySelector('[class*="close"]') || document.querySelector('[class*="Close"]');
          if (closeBtn) { closeBtn.click(); return true; }
          return false;
        })()
      `);
      await sleep(1000);
    }
    
    // Step 2: Now search again and click the % button (last odds button)
    // First re-navigate to search to reset
    console.log(`  Navigating back to search for ${mid}...`);
    await send('Page.navigate', { url: 'https://bet.hkjc.com/ch/football/results#search' });
    await sleep(3000);
    
    const clickOddsScript = `
      (() => {
        const rows = document.querySelectorAll('.fb-results-match-results .table-row');
        for (const row of rows) {
          if (row.classList.contains('table-header')) continue;
          const cells = row.querySelectorAll('.table-cell');
          if (cells[1]?.innerText?.trim() !== '${mid}') continue;
          
          const detailCell = row.querySelector('.detail-cell');
          if (!detailCell) return { ok: false, error: 'no detail-cell' };
          
          const buttons = detailCell.querySelectorAll(':scope > div');
          // Second button should be the % odds button
          if (buttons.length >= 2) {
            buttons[buttons.length - 1].click();
            return { ok: true, action: 'clicked_odds', matchId: '${mid}' };
          }
          return { ok: false, error: 'not enough buttons: ' + buttons.length };
        }
        return { ok: false, error: 'match row not found', matchId: '${mid}' };
      })()
    `;
    
    result = await evaluate(clickOddsScript);
    console.log(`  Click odds result:`, JSON.stringify(result));
    
    if (result?.ok) {
      await sleep(3000);
      
      // Extract odds
      const oddsScript = `
        (() => {
          const WANT = ['主客和','半場主客和','讓球','半場讓球','入球大細','半場入球大細','開出角球大細','半場開出角球大細'];
          const SKIP_PREFIX = ['即場','同場過關'];
          const out = {};
          const allText = document.body.innerText;
          
          for (const name of WANT) {
            // Skip if the section contains 即場
            if (SKIP_PREFIX.some(s => name.includes(s))) {
              out[name] = [];
              continue;
            }
            
            const idx = allText.indexOf(name);
            if (idx < 0) { out[name] = []; continue; }
            
            const slice = allText.slice(idx, idx + 2500);
            
            // Check if this is the 即場 variant
            const beforeHeader = allText.slice(Math.max(0, idx - 50), idx);
            const afterHeader = allText.slice(idx, idx + name.length + 30);
            if (/即場/.test(afterHeader) && !beforeHeader.includes('最後賠率')) {
              // Try to find the non-即場 version
              const nonLiveIdx = allText.indexOf(name, idx + 1);
              if (nonLiveIdx > 0 && !allText.slice(nonLiveIdx - 50, nonLiveIdx + name.length + 30).includes('即場')) {
                const slice2 = allText.slice(nonLiveIdx, nonLiveIdx + 2500);
                // parse slice2
                const lines = [];
                if (name.includes('主客和')) {
                  const hda = [...slice2.matchAll(/(主隊勝|和|客隊勝)\\\\s*[\\\\(（][^\\\\)）]+[\\\\)）]?\\\\s*(\\\\d+\\\\.?\\\\d*)/g)];
                  if (hda.length) out[name] = hda.map(x => ({ selection: x[1], odds: parseFloat(x[2]) }));
                  else {
                    const hda2 = [...slice2.matchAll(/(主隊勝|和|客隊勝)[^\\\\d]*?(\\\\d+\\\\.?\\\\d*)/g)];
                    out[name] = hda2.length ? hda2.map(x => ({ selection: x[1], odds: parseFloat(x[2]) })) : [];
                  }
                } else {
                  const lineRe = /(\\\\[[^\\\\]]+\\\\]|[-+]?\\\\d+(?:\\\\.\\\\d+)?(?:\\\/[-+]?\\\\d+(?:\\\\.\\\\d+)?)?)\\\\s+(\\\\d+\\\\.\\\\d+)/g;
                  let m;
                  while ((m = lineRe.exec(slice2)) !== null) {
                    lines.push({ line: m[1], odds: parseFloat(m[2]) });
                  }
                  out[name] = lines;
                }
                continue;
              }
              out[name] = [];
              continue;
            }
            
            const lines = [];
            if (name.includes('主客和')) {
              // Try 1X2 format
              const hda = [...slice.matchAll(/(主隊勝|和|客隊勝)\\\\s*[\\\\(（][^\\\\)）]+[\\\\)）]?\\\\s*(\\\\d+\\\\.?\\\\d*)/g)];
              if (hda.length) {
                out[name] = hda.map(x => ({ selection: x[1], odds: parseFloat(x[2]) }));
              } else {
                const hda2 = [...slice.matchAll(/(主隊勝|和|客隊勝)[^\\\\d]*?(\\\\d+\\\\.?\\\\d*)/g)];
                out[name] = hda2.length ? hda2.map(x => ({ selection: x[1], odds: parseFloat(x[2]) })) : [];
              }
            } else {
              // Line-based: [8.5] 1.64 or -0.5/-1 2.01
              const lineRe = /(\\\\[[^\\\\]]+\\\\]|[-+]?\\\\d+(?:\\\\.\\\\d+)?(?:\\\/[-+]?\\\\d+(?:\\\\.\\\\d+)?)?)\\\\s+(\\\\d+\\\\.\\\\d+)/g;
              let m;
              while ((m = lineRe.exec(slice)) !== null) {
                lines.push({ line: m[1], odds: parseFloat(m[2]) });
              }
              out[name] = lines;
            }
          }
          return out;
        })()
      `;
      
      const odds = await evaluate(oddsScript);
      console.log(`  Odds extracted:`);
      for (const [k, v] of Object.entries(odds)) {
        console.log(`    ${k}: ${v.length} items`);
      }
      
      // Transform odds into proper format and update match
      match.odds_closing = odds;
      
      // Close odds panel
      await evaluate(`document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))`);
      await sleep(1500);
    } else {
      console.log(`  FAILED to click odds button for ${mid}`);
    }
    
    save();
    await sleep(1000);
  }
  
  console.log('\n=== ALL DONE ===');
  process.exit(0);
}

main().catch(err => {
  console.error('Fatal:', err);
  process.exit(1);
});
