const express = require('express');
const fs = require('fs');
const path = require('path');
const { v4: uuidv4 } = require('uuid');

const app = express();
const PORT = 80;
const DATA_DIR = '/data';

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public'), { index: false }));

// --- Atomic write with promise queue ---

let writeQueue = Promise.resolve();

function enqueueWrite(fn) {
  writeQueue = writeQueue.then(fn).catch(fn);
  return writeQueue;
}

function dataFile(projectId) {
  // Sanitize: only allow uuid chars
  const safe = projectId.replace(/[^a-f0-9-]/gi, '');
  return path.join(DATA_DIR, safe + '.json');
}

function readData(projectId) {
  try {
    const raw = fs.readFileSync(dataFile(projectId), 'utf8');
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function writeData(projectId, data) {
  return enqueueWrite(() => {
    const file = dataFile(projectId);
    const tmp = file + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
    fs.renameSync(tmp, file);
  });
}

function createProjectData(name) {
  const tables = [];
  const now = new Date().toISOString();
  for (let i = 1; i <= 15; i++) {
    tables.push({
      id: uuidv4(),
      number: i,
      label: `${i}桌`,
      tag: null,
      createdAt: now,
    });
  }
  return { name: name || '我的婚礼', guests: [], tables };
}

// Ensure data dir exists
try {
  fs.mkdirSync(DATA_DIR, { recursive: true });
} catch {}

// --- Middleware to load project ---

function requireProject(req, res, next) {
  const data = readData(req.params.projectId);
  if (!data) return res.status(404).json({ error: 'Project not found' });
  req.projectData = data;
  req.projectId = req.params.projectId;
  next();
}

// --- API Routes ---

// Root: landing page
app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'landing.html'));
});

// POST create project
app.post('/api/projects', async (req, res) => {
  const id = uuidv4();
  const name = (req.body.name || '').trim() || '我的婚礼';
  const data = createProjectData(name);
  await writeData(id, data);
  res.status(201).json({ id, name });
});

// GET full data
app.get('/api/p/:projectId/data', requireProject, (req, res) => {
  res.json(req.projectData);
});

// POST add guest
app.post('/api/p/:projectId/guests', requireProject, async (req, res) => {
  const { name, side } = req.body;
  if (!name || !name.trim()) return res.status(400).json({ error: 'Name is required' });
  if (!side || (side !== '男方' && side !== '女方')) return res.status(400).json({ error: 'Side must be 男方 or 女方' });

  const data = req.projectData;
  const guest = {
    id: uuidv4(),
    name: name.trim(),
    side,
    tableId: null,
    seatLabel: null,
    pending: false,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
  data.guests.push(guest);
  await writeData(req.projectId, data);
  res.status(201).json(guest);
});

// PUT update guest
app.put('/api/p/:projectId/guests/:id', requireProject, async (req, res) => {
  const data = req.projectData;
  const guest = data.guests.find(g => g.id === req.params.id);
  if (!guest) return res.status(404).json({ error: 'Guest not found' });

  const { name, side, tableId, seatLabel, pending } = req.body;
  if (name !== undefined) guest.name = name.trim();
  if (side !== undefined) guest.side = side;
  if (tableId !== undefined) guest.tableId = tableId;
  if (seatLabel !== undefined) guest.seatLabel = seatLabel;
  if (pending !== undefined) guest.pending = !!pending;
  guest.updatedAt = new Date().toISOString();

  await writeData(req.projectId, data);
  res.json(guest);
});

// DELETE guest
app.delete('/api/p/:projectId/guests/:id', requireProject, async (req, res) => {
  const data = req.projectData;
  const idx = data.guests.findIndex(g => g.id === req.params.id);
  if (idx === -1) return res.status(404).json({ error: 'Guest not found' });
  data.guests.splice(idx, 1);
  await writeData(req.projectId, data);
  res.status(204).end();
});

// POST assign guest to table
app.post('/api/p/:projectId/guests/:id/assign', requireProject, async (req, res) => {
  const { tableId, seatLabel } = req.body;
  if (!tableId) return res.status(400).json({ error: 'tableId is required' });

  const data = req.projectData;
  const guest = data.guests.find(g => g.id === req.params.id);
  if (!guest) return res.status(404).json({ error: 'Guest not found' });

  const table = data.tables.find(t => t.id === tableId);
  if (!table) return res.status(404).json({ error: 'Table not found' });

  if (seatLabel) {
    const conflict = data.guests.find(
      g => g.tableId === tableId && g.seatLabel === seatLabel && g.id !== guest.id
    );
    if (conflict) {
      return res.status(409).json({ error: `Seat "${seatLabel}" is already taken by ${conflict.name}` });
    }
  }

  guest.tableId = tableId;
  guest.seatLabel = seatLabel || null;
  guest.updatedAt = new Date().toISOString();

  await writeData(req.projectId, data);
  res.json(guest);
});

// POST swap seats between two guests
app.post('/api/p/:projectId/guests/swap-seats', requireProject, async (req, res) => {
  const { guestIdA, guestIdB } = req.body;
  if (!guestIdA || !guestIdB) return res.status(400).json({ error: 'guestIdA and guestIdB are required' });
  if (guestIdA === guestIdB) return res.status(400).json({ error: 'Cannot swap a guest with itself' });

  const data = req.projectData;
  const guestA = data.guests.find(g => g.id === guestIdA);
  const guestB = data.guests.find(g => g.id === guestIdB);
  if (!guestA) return res.status(404).json({ error: 'Guest A not found' });
  if (!guestB) return res.status(404).json({ error: 'Guest B not found' });

  const now = new Date().toISOString();
  const oldA = { tableId: guestA.tableId, seatLabel: guestA.seatLabel };
  const oldB = { tableId: guestB.tableId, seatLabel: guestB.seatLabel };

  if (oldA.tableId) {
    guestB.tableId = oldA.tableId;
    guestB.seatLabel = oldA.seatLabel;
  } else {
    guestB.tableId = null;
    guestB.seatLabel = null;
  }

  if (oldB.tableId) {
    guestA.tableId = oldB.tableId;
    guestA.seatLabel = oldB.seatLabel;
  } else {
    guestA.tableId = null;
    guestA.seatLabel = null;
  }

  guestA.updatedAt = now;
  guestB.updatedAt = now;

  await writeData(req.projectId, data);
  res.json({ guestA, guestB });
});

// POST unassign guest from table
app.post('/api/p/:projectId/guests/:id/unassign', requireProject, async (req, res) => {
  const data = req.projectData;
  const guest = data.guests.find(g => g.id === req.params.id);
  if (!guest) return res.status(404).json({ error: 'Guest not found' });

  guest.tableId = null;
  guest.seatLabel = null;
  guest.updatedAt = new Date().toISOString();

  await writeData(req.projectId, data);
  res.json(guest);
});

// POST add table
app.post('/api/p/:projectId/tables', requireProject, async (req, res) => {
  const data = req.projectData;
  const maxNumber = data.tables.reduce((max, t) => Math.max(max, t.number), 0);
  const table = {
    id: uuidv4(),
    number: maxNumber + 1,
    label: (req.body.label || `${maxNumber + 1}桌`).trim(),
    tag: req.body.tag || null,
    createdAt: new Date().toISOString(),
  };
  data.tables.push(table);
  await writeData(req.projectId, data);
  res.status(201).json(table);
});

// PUT update table
app.put('/api/p/:projectId/tables/:id', requireProject, async (req, res) => {
  const data = req.projectData;
  const table = data.tables.find(t => t.id === req.params.id);
  if (!table) return res.status(404).json({ error: 'Table not found' });

  const { number, label, tag } = req.body;
  if (number !== undefined) table.number = number;
  if (label !== undefined) table.label = label.trim();
  if (tag !== undefined) table.tag = tag;

  await writeData(req.projectId, data);
  res.json(table);
});

// DELETE table (only if empty AND has the highest table number)
app.delete('/api/p/:projectId/tables/:id', requireProject, async (req, res) => {
  const data = req.projectData;
  const table = data.tables.find(t => t.id === req.params.id);
  if (!table) return res.status(404).json({ error: 'Table not found' });

  const hasGuests = data.guests.some(g => g.tableId === req.params.id);
  if (hasGuests) {
    return res.status(409).json({ error: 'Cannot delete table with assigned guests' });
  }

  const maxNumber = data.tables.reduce((max, t) => Math.max(max, t.number), 0);
  if (table.number !== maxNumber) {
    return res.status(409).json({ error: '只能删除最后一桌' });
  }

  const idx = data.tables.indexOf(table);
  data.tables.splice(idx, 1);
  await writeData(req.projectId, data);
  res.status(204).end();
});

// POST swap all guests between two tables
app.post('/api/p/:projectId/tables/swap', requireProject, async (req, res) => {
  const { tableIdA, tableIdB } = req.body;
  if (!tableIdA || !tableIdB) return res.status(400).json({ error: 'tableIdA and tableIdB are required' });
  if (tableIdA === tableIdB) return res.status(400).json({ error: 'Cannot swap a table with itself' });

  const data = req.projectData;
  const tableA = data.tables.find(t => t.id === tableIdA);
  const tableB = data.tables.find(t => t.id === tableIdB);
  if (!tableA) return res.status(404).json({ error: 'Table A not found' });
  if (!tableB) return res.status(404).json({ error: 'Table B not found' });

  // Swap tags between tables
  const tempTag = tableA.tag;
  tableA.tag = tableB.tag;
  tableB.tag = tempTag;

  const now = new Date().toISOString();
  for (const guest of data.guests) {
    if (guest.tableId === tableIdA) {
      guest.tableId = tableIdB;
      guest.updatedAt = now;
    } else if (guest.tableId === tableIdB) {
      guest.tableId = tableIdA;
      guest.updatedAt = now;
    }
  }

  await writeData(req.projectId, data);
  res.json({ tableA, tableB });
});

// PUT update project name
app.put('/api/p/:projectId', requireProject, async (req, res) => {
  const data = req.projectData;
  if (req.body.name !== undefined) data.name = req.body.name.trim();
  await writeData(req.projectId, data);
  res.json({ name: data.name });
});

// POST batch add guests
app.post('/api/p/:projectId/guests/batch', requireProject, async (req, res) => {
  const { names, side } = req.body;
  if (!Array.isArray(names) || names.length === 0) return res.status(400).json({ error: 'names array is required' });
  if (!side || (side !== '男方' && side !== '女方')) return res.status(400).json({ error: 'Side must be 男方 or 女方' });

  const data = req.projectData;
  const now = new Date().toISOString();
  const added = [];
  for (const raw of names) {
    const name = String(raw).trim();
    if (!name) continue;
    const guest = {
      id: uuidv4(),
      name,
      side,
      tableId: null,
      seatLabel: null,
      pending: false,
      createdAt: now,
      updatedAt: now,
    };
    data.guests.push(guest);
    added.push(guest);
  }
  await writeData(req.projectId, data);
  res.status(201).json(added);
});

// GET export CSV
app.get('/api/p/:projectId/export/csv', requireProject, (req, res) => {
  const data = req.projectData;
  const tableMap = {};
  data.tables.forEach(t => { tableMap[t.id] = t; });

  const BOM = '\uFEFF';
  const header = '宾客姓名,关系,桌号,桌子标签,座位\n';
  const rows = data.guests.map(g => {
    const table = g.tableId ? tableMap[g.tableId] : null;
    const tableName = table ? `${table.number}号桌` : '';
    const tableTag = table && table.tag ? table.tag : '';
    const seat = g.seatLabel || '';
    return [g.name, g.side || '', tableName, tableTag, seat]
      .map(v => `"${String(v).replace(/"/g, '""')}"`)
      .join(',');
  }).join('\n');

  res.setHeader('Content-Type', 'text/csv; charset=utf-8');
  res.setHeader('Content-Disposition', 'attachment; filename="seating.csv"');
  res.send(BOM + header + rows);
});

// SPA: serve index.html for /p/:id routes
app.get('/p/*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.listen(PORT, () => {
  console.log(`Wedding Seating server running on port ${PORT}`);
});
