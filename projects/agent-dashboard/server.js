#!/usr/bin/env node
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');
const readline = require('readline');

const PORT = 3847;
const SESSIONS_DIRS = [
  '/home/jingtao/.openclaw/agents/worker-leader/sessions',
  '/home/jingtao/.openclaw/agents/worker/sessions',
];
const KB_DIR = '/home/jingtao/.openclaw/workspace/memory/kb';
const KB_JSON = '/home/jingtao/.openclaw/workspace-worker-leader/kb.json';
const STATUS_JSON = '/home/jingtao/.openclaw/workspace-worker-leader/status.json';
const WORKER_LOGS_BASE = '/home/jingtao/.openclaw/workspace-worker-leader/logs';
const ORCHESTRATOR_STATE = '/home/jingtao/.openclaw/workspace/subagent-tracking/orchestrator-state.json';
const STATIC_DIR = path.join(__dirname);

// ---------- JSONL session parser ----------
async function parseSession(filePath) {
  return new Promise((resolve) => {
    const entries = [];
    try {
      const rl = readline.createInterface({ input: fs.createReadStream(filePath), crlfDelay: Infinity });
      rl.on('line', (line) => {
        try { if (line.trim()) entries.push(JSON.parse(line)); } catch {}
      });
      rl.on('close', () => resolve(entries));
      rl.on('error', () => resolve(entries));
    } catch {
      resolve(entries);
    }
  });
}

function extractWorkerInfo(entries, sessionFile) {
  const sessionId = path.basename(sessionFile, '.jsonl').substring(0, 8);
  let name = null;
  let status = 'unknown';
  let task = null;
  let taskFull = null;
  let startTime = null;
  let endTime = null;
  let channel = null;
  let blockedDetail = null;
  let kbGuidanceText = null;
  let completeSummaryFull = null;
  const timeline = [];

  for (const e of entries) {
    if (e.type === 'session') {
      startTime = e.timestamp;
      timeline.push({ time: e.timestamp, event: 'spawned', detail: 'Session started' });
    }
    if (e.type === 'message') {
      const msg = e.message || {};
      const role = msg.role;
      const content = msg.content || [];
      let text = '';
      for (const c of content) {
        if (c && c.type === 'text') text += c.text || '';
      }

      if (role === 'user' && !task) {
        const taskFullMatch = text.match(/\[Subagent Task\][:\s]*([\s\S]{1,3000})/);
        if (taskFullMatch) {
          taskFull = taskFullMatch[1].trim();
          // Prefer ## Your Task / ## Task section over ## Your Identity boilerplate
          const namedSection = taskFull.match(/##\s*(?:Your\s+)?Task[:\s]*\n([\s\S]+?)(?=\n##|$)/i);
          if (namedSection) {
            task = namedSection[1].trim().replace(/\n/g, ' ').substring(0, 120);
          } else {
            // Strip leading markdown headers and boilerplate identity lines
            task = taskFull
              .replace(/^#+\s*.+\n?/gm, '')          // remove ## headings
              .replace(/^You are Worker[^\n]*\n?/im, '') // remove "You are Worker-N"
              .trim()
              .replace(/\n/g, ' ')
              .substring(0, 120);
          }
        }
        const workerMatch = text.match(/You are (Worker[-\s]\d+|[\w-]+worker[\w-]*)/i);
        if (workerMatch) name = workerMatch[1];
        const channelMatch = text.match(/\*\*Channel:\*\*\s*(.+?)(?:\n|\()/);
        if (channelMatch) channel = channelMatch[1].trim();
      }

      if (role === 'assistant') {
        if (text.includes('BLOCKED:') || text.includes('BLOCKED ')) {
          status = 'blocked';
          const blockMatch = text.match(/BLOCKED[:\s]+(.+?)(?:\n|$)/);
          const detail = blockMatch ? blockMatch[1].trim() : 'Blocked';
          timeline.push({ time: e.timestamp, event: 'blocked', detail });
          if (!blockedDetail) blockedDetail = text.substring(0, 1500);
        }
        if (text.includes('LEARNED:')) {
          const learnedMatch = text.match(/LEARNED[:\s]+(.+?)(?:\n|$)/);
          const detail = learnedMatch ? learnedMatch[1].trim().substring(0, 100) : 'Uploaded knowledge';
          timeline.push({ time: e.timestamp, event: 'learned', detail });
        }
        if (text.includes('KB_GUIDANCE') || text.includes('KB guidance') || text.includes('GUIDANCE:')) {
          timeline.push({ time: e.timestamp, event: 'steered', detail: 'Received KB guidance' });
          status = 'running';
        }
        const isComplete = text.includes('TASK_COMPLETE')
          || text.includes('DONE:')
          || /task[\s_]*complete/i.test(text)
          || /completed\s+successfully/i.test(text)
          || /successfully\s+completed/i.test(text)
          || /all\s+\d+\s+keys\s+solved/i.test(text)
          || /✅.*task\s+complete|task\s+complete.*✅/i.test(text);
        if (isComplete) {
          status = 'complete';
          endTime = e.timestamp;
          if (!completeSummaryFull) completeSummaryFull = text.substring(0, 1500);
          const summaryMatch = text.match(/(?:SUMMARY|DONE)[:\s]+(.+?)(?:\n|ARTIFACTS|$)/s);
          timeline.push({ time: e.timestamp, event: 'complete', detail: summaryMatch ? summaryMatch[1].trim().substring(0, 100) : 'Task complete' });
        }
      }

      if (role === 'user' && (text.includes('KB_GUIDANCE') || text.includes('GUIDANCE:'))) {
        status = 'running';
        if (!kbGuidanceText) kbGuidanceText = text.substring(0, 1500);
        const guidanceMatch = text.match(/(?:KB_GUIDANCE|GUIDANCE)[:\s]+(.+?)(?:\n|$)/);
        timeline.push({ time: e.timestamp, event: 'steered', detail: guidanceMatch ? guidanceMatch[1].trim().substring(0, 100) : 'Steered by KB' });
      }
    }
  }

  if (status === 'unknown' && entries.length > 1) status = 'running';

  // Heuristic: if session file hasn't been updated in 5+ minutes and still "running" or "blocked",
  // it's likely finished (mode=run sessions don't always emit TASK_COMPLETE)
  if ((status === 'running' || status === 'blocked') && endTime === null) {
    const lastEntry = entries[entries.length - 1];
    const lastTs = lastEntry?.timestamp || lastEntry?.message?.timestamp;
    if (lastTs) {
      const age = Date.now() - new Date(lastTs).getTime();
      if (age > 5 * 60 * 1000) {
        status = 'complete';
        endTime = lastTs;
        timeline.push({ time: lastTs, event: 'complete', detail: 'Session ended (inferred)' });
      }
    }
  }

  return {
    sessionId,
    name: name || `worker-${sessionId}`,
    status,
    task: task || 'Unknown task',
    taskFull,
    channel: channel || null,
    startTime,
    endTime,
    timeline,
    blockedDetail,
    kbGuidanceText,
    completeSummaryFull,
    elapsed: startTime && endTime
      ? Math.round((new Date(endTime) - new Date(startTime)) / 1000)
      : startTime
        ? Math.round((Date.now() - new Date(startTime)) / 1000)
        : null,
  };
}

// ---------- Round grouping ----------
function generateRoundLabel(sessions) {
  const allTasks = sessions.map(s => s.task || '').join(' ');
  const allTasksFull = sessions.map(s => s.taskFull || '').join(' ');
  const corpus = allTasksFull || allTasks;
  const patterns = [
    [/number\s+guess(?:ing)?|guess.*game/i, 'Number Guessing Game'],
    [/incoming.?webhook/i, 'Incoming Webhooks'],
    [/outgoing.?webhook/i, 'Outgoing Webhooks'],
    [/monitor.?bot/i, 'Monitor Bots'],
    [/slash.?command/i, 'Slash Commands'],
    [/webhook/i, 'Webhooks'],
    [/mattermost/i, 'Mattermost Setup'],
    [/github/i, 'GitHub Tasks'],
    [/discord/i, 'Discord Tasks'],
  ];
  for (const [re, label] of patterns) {
    if (re.test(corpus)) return label;
  }
  // Fall back: strip markdown/boilerplate from task, take first few meaningful words
  const firstTask = sessions[0]?.task || '';
  const cleaned = firstTask
    .replace(/^#+\s*/gm, '')
    .replace(/\*\*/g, '')
    .replace(/^(create|set up|configure|implement|add|you are worker[\s-]*\d+)\s*/i, '')
    .trim();
  const words = cleaned.split(/\s+/).slice(0, 5).join(' ');
  return words || 'Workers';
}

function groupIntoRounds(workers) {
  if (!workers.length) return [];

  const rounds = [];
  let currentRound = [workers[0]];
  let roundStartTime = new Date(workers[0].startTime || 0).getTime();

  for (let i = 1; i < workers.length; i++) {
    const wTime = new Date(workers[i].startTime || 0).getTime();
    if (wTime - roundStartTime <= 60000) {
      currentRound.push(workers[i]);
    } else {
      rounds.push(currentRound);
      currentRound = [workers[i]];
      roundStartTime = wTime;
    }
  }
  rounds.push(currentRound);

  return rounds.map((sessions, idx) => {
    const startTimes = sessions.map(s => new Date(s.startTime || 0).getTime()).filter(t => t > 0);
    const endTimes = sessions.map(s => s.endTime ? new Date(s.endTime).getTime() : null).filter(Boolean);
    const roundStart = startTimes.length ? new Date(Math.min(...startTimes)).toISOString() : null;
    const roundEnd = endTimes.length === sessions.length ? new Date(Math.max(...endTimes)).toISOString() : null;

    const complete = sessions.filter(s => s.status === 'complete').length;
    const blocked = sessions.filter(s => s.status === 'blocked').length;
    const running = sessions.filter(s => s.status === 'running').length;
    const total = sessions.length;

    let statusSummary;
    if (complete === total) statusSummary = `${total}/${total} complete`;
    else if (blocked > 0) statusSummary = `${blocked}/${total} blocked`;
    else if (running > 0) statusSummary = `${running}/${total} running`;
    else statusSummary = `${complete}/${total} complete`;

    return {
      id: `round-${idx + 1}`,
      roundNum: idx + 1,
      startTime: roundStart,
      endTime: roundEnd,
      label: generateRoundLabel(sessions),
      statusSummary,
      hasRunning: running > 0,
      hasBlocked: blocked > 0,
      allComplete: complete === total && total > 0,
      sessions,
    };
  });
}

// ---------- KB parser ----------
function parseKBFile(content, filename) {
  const entries = [];
  const rawSections = content.split(/\n---\n/);
  for (const rawSection of rawSections) {
    const subSections = rawSection.split(/(?=^## )/m);
    for (const section of subSections) {
      const headerMatch = section.match(/^##\s+(.+?)\s*(?:\((\d{4}-\d{2}-\d{2})\))?\s*$/m);
      if (!headerMatch) continue;
      const title = headerMatch[1].trim();
      const date = headerMatch[2] || null;
      const field = (label) => {
        const re = new RegExp(`\\*\\*${label}:\\*\\*\\s*([\\s\\S]+?)(?=\\n\\*\\*[A-Za-z]+:\\*\\*|$)`);
        const m = section.match(re);
        return m ? m[1].trim() : '';
      };
      const context  = field('Context');
      const problem  = field('Problem');
      const solution = field('Solution');
      const source   = field('Source');
      if (title && (problem || solution)) {
        entries.push({ title, date, context, problem, solution, source, file: filename });
      }
    }
  }
  return entries;
}

// ---------- Worker log file parser (logs/{leaderSid}/{workerSid}.jsonl) ----------
function parseWorkerLogFile(filePath) {
  const entries = [];
  if (!fs.existsSync(filePath)) return entries;
  try {
    const lines = fs.readFileSync(filePath, 'utf8').split('\n');
    for (const line of lines) {
      if (!line.trim()) continue;
      try { entries.push(JSON.parse(line)); } catch {}
    }
  } catch {}
  return entries;
}

function mergeLogEntriesIntoTimeline(worker, logEntries) {
  for (const entry of logEntries) {
    const time = entry.at || null;
    if (entry.type === 'help') {
      worker.timeline.push({
        time,
        event: 'help_requested',
        detail: `context=${(entry.context || '').substring(0, 80)} tried=${(entry.tried || '').substring(0, 60)}`,
      });
    } else {
      // learned entry (fact, pattern, failure, heuristic)
      worker.timeline.push({
        time,
        event: 'learned',
        detail: `[${entry.type || 'fact'}] ${(entry.content || '').substring(0, 100)}`,
      });
    }
  }
  // Re-sort timeline by time
  worker.timeline.sort((a, b) => {
    if (!a.time) return -1;
    if (!b.time) return 1;
    return new Date(a.time) - new Date(b.time);
  });
}

// ---------- Link leader sessions to their spawned workers ----------
async function buildLeaderRounds() {
  const leaderDir = SESSIONS_DIRS[0];
  const workerDir = SESSIONS_DIRS[1];
  if (!fs.existsSync(leaderDir)) return [];

  // Load worker sessions.json: childSessionKey → sessionId
  const workerSessionsJsonPath = path.join(workerDir, 'sessions.json');
  let childKeyToSessionId = {};
  if (fs.existsSync(workerSessionsJsonPath)) {
    try {
      const raw = JSON.parse(fs.readFileSync(workerSessionsJsonPath, 'utf8'));
      for (const [k, v] of Object.entries(raw)) {
        if (k.includes(':subagent:')) {
          childKeyToSessionId[k] = typeof v === 'object' ? v.sessionId : v;
        }
      }
    } catch {}
  }

  const rounds = [];
  const leaderFiles = fs.readdirSync(leaderDir).filter(f => f.endsWith('.jsonl'));

  for (const leaderFile of leaderFiles) {
    const leaderEntries = await parseSession(path.join(leaderDir, leaderFile));
    if (leaderEntries.length < 2) continue;

    // Extract childSessionKeys from sessions_spawn toolResults
    const childSessionKeys = [];
    for (const e of leaderEntries) {
      if (e.type !== 'message') continue;
      const role = e.message?.role;
      if (role !== 'toolResult') continue;
      for (const block of e.message?.content || []) {
        const text = block?.text || '';
        const match = text.match(/"childSessionKey"\s*:\s*"([^"]+)"/);
        if (match) childSessionKeys.push(match[1]);
      }
    }

    if (childSessionKeys.length === 0) continue; // not an orchestrator session

    // Load each worker session
    const workers = [];
    for (const childKey of childSessionKeys) {
      const sessionId = childKeyToSessionId[childKey];
      if (!sessionId) continue;
      const workerFile = path.join(workerDir, `${sessionId}.jsonl`);
      if (!fs.existsSync(workerFile)) continue;
      const entries = await parseSession(workerFile);
      if (entries.length > 1) {
        workers.push(extractWorkerInfo(entries, `${sessionId}.jsonl`));
      }
    }

    if (workers.length === 0) continue;

    // Merge worker log files (logs/{leaderSid}/{workerSid}.jsonl) into timelines
    const leaderSid = path.basename(leaderFile, '.jsonl');
    const logDir = path.join(WORKER_LOGS_BASE, leaderSid);
    if (fs.existsSync(logDir)) {
      for (const worker of workers) {
        // Try matching by full sessionId or short prefix
        const fullSid = path.basename(leaderFile, '.jsonl').includes(worker.sessionId)
          ? worker.sessionId
          : null;
        // Scan log dir for files matching this worker's session
        try {
          const logFiles = fs.readdirSync(logDir).filter(f => f.endsWith('.jsonl'));
          for (const lf of logFiles) {
            const logSid = path.basename(lf, '.jsonl');
            // Match: the childSessionKey maps to a sessionId which is used as log filename
            // We match by checking if the worker's full session filename starts with the log sid or vice versa
            if (childSessionKeys.some(k => {
              const mappedSid = childKeyToSessionId[k];
              return mappedSid && logSid.startsWith(mappedSid.substring(0, 8));
            })) {
              // Find which worker this log belongs to
              const matchedWorker = workers.find(w => logSid.startsWith(w.sessionId));
              if (matchedWorker) {
                const logEntries = parseWorkerLogFile(path.join(logDir, lf));
                mergeLogEntriesIntoTimeline(matchedWorker, logEntries);
              }
            }
          }
        } catch {}
      }
    }

    // Extract leader start time from its session entry
    const leaderSessionEntry = leaderEntries.find(e => e.type === 'session');
    const leaderStart = leaderSessionEntry?.timestamp || null;

    const complete = workers.filter(w => w.status === 'complete').length;
    const blocked  = workers.filter(w => w.status === 'blocked').length;
    const running  = workers.filter(w => w.status === 'running').length;
    const total    = workers.length;
    const allComplete = complete === total && total > 0;
    const endTimes = workers.map(w => w.endTime ? new Date(w.endTime).getTime() : null).filter(Boolean);

    let statusSummary;
    if (complete === total) statusSummary = `${total}/${total} complete`;
    else if (blocked > 0)   statusSummary = `${blocked}/${total} blocked`;
    else if (running > 0)   statusSummary = `${running}/${total} running`;
    else                    statusSummary = `${complete}/${total} complete`;

    rounds.push({
      id: `round-${path.basename(leaderFile, '.jsonl').substring(0, 8)}`,
      roundNum: rounds.length + 1,
      leaderId: path.basename(leaderFile, '.jsonl'),
      startTime: leaderStart,
      endTime: allComplete && endTimes.length ? new Date(Math.max(...endTimes)).toISOString() : null,
      label: generateRoundLabel(workers),
      statusSummary,
      hasRunning: running > 0,
      hasBlocked: blocked > 0,
      allComplete,
      sessions: workers,
    });
  }

  // Sort by startTime
  rounds.sort((a, b) => new Date(a.startTime || 0) - new Date(b.startTime || 0));
  rounds.forEach((r, i) => r.roundNum = i + 1);
  return rounds;
}

// ---------- Route handlers ----------
async function handleWorkers(res) {
  try {
    const linkedRounds = await buildLeaderRounds();
    if (linkedRounds.length) return sendJSON(res, { rounds: linkedRounds });

    // Fallback: no leader→worker links found, show worker sessions grouped by time
    const seenFiles = new Set();
    const workers = [];
    for (const dir of SESSIONS_DIRS) {
      if (!fs.existsSync(dir)) continue;
      const files = fs.readdirSync(dir).filter(f => f.endsWith('.jsonl') && !seenFiles.has(f));
      for (const file of files) {
        seenFiles.add(file);
        const filePath = path.join(dir, file);
        const entries = await parseSession(filePath);
        if (entries.length > 1) {
          workers.push(extractWorkerInfo(entries, file));
        }
      }
    }
    if (!workers.length) return sendJSON(res, { rounds: [] });

    // Sort by startTime ascending for grouping, then group
    workers.sort((a, b) => new Date(a.startTime || 0) - new Date(b.startTime || 0));
    const rounds = groupIntoRounds(workers);

    sendJSON(res, { rounds });
  } catch (err) {
    sendJSON(res, { error: err.message }, 500);
  }
}

function handleKB(res) {
  try {
    const entries = [];

    if (fs.existsSync(KB_JSON)) {
      const kb = JSON.parse(fs.readFileSync(KB_JSON, 'utf8'));
      for (const e of (kb.entries || [])) {
        entries.push({
          title: e.content ? e.content.substring(0, 80) : 'Entry',
          date: e.at ? e.at.substring(0, 10) : null,
          context: e.context || '',
          problem: e.type === 'failure' ? e.content : '',
          solution: e.workaround || (e.type !== 'failure' ? e.content : ''),
          source: e.source || '',
          type: e.type || 'fact',
          file: 'kb.json',
        });
      }
    }

    // Include scan status if available
    let scanStatus = null;
    if (fs.existsSync(STATUS_JSON)) {
      try {
        scanStatus = JSON.parse(fs.readFileSync(STATUS_JSON, 'utf8'));
      } catch {}
    }

    sendJSON(res, { entries, scanStatus });
  } catch (err) {
    sendJSON(res, { error: err.message }, 500);
  }
}

function handleOrchestratorState(res) {
  try {
    if (!fs.existsSync(ORCHESTRATOR_STATE)) return sendJSON(res, null);
    const state = JSON.parse(fs.readFileSync(ORCHESTRATOR_STATE, 'utf8'));
    sendJSON(res, state);
  } catch (err) {
    sendJSON(res, { error: err.message }, 500);
  }
}

function handleStatic(req, res) {
  let filePath = req.url === '/' ? '/index.html' : req.url;
  filePath = path.join(STATIC_DIR, filePath);
  if (!filePath.startsWith(STATIC_DIR)) {
    res.writeHead(403); res.end('Forbidden'); return;
  }
  fs.readFile(filePath, (err, data) => {
    if (err) { res.writeHead(404); res.end('Not found'); return; }
    const ext = path.extname(filePath);
    const mimeTypes = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml' };
    res.writeHead(200, { 'Content-Type': mimeTypes[ext] || 'application/octet-stream' });
    res.end(data);
  });
}

function sendJSON(res, data, status = 200) {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Cache-Control': 'no-cache',
  });
  res.end(body);
}

// ---------- Server ----------
const server = http.createServer(async (req, res) => {
  const url = req.url.split('?')[0];
  if (req.method === 'GET') {
    if (url === '/api/workers') return handleWorkers(res);
    if (url === '/api/kb') return handleKB(res);
    if (url === '/api/orchestrator-state') return handleOrchestratorState(res);
    return handleStatic(req, res);
  }
  res.writeHead(405); res.end('Method Not Allowed');
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`Agent Dashboard server running at http://0.0.0.0:${PORT}`);
});
