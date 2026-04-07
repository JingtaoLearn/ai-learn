const express = require('express');
const fs = require('fs');
const path = require('path');
const { v4: uuidv4 } = require('uuid');

const app = express();
const PORT = 80;
const DATA_FILE = '/data/seating.json';

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// --- Atomic write with promise queue ---

let writeQueue = Promise.resolve();

function enqueueWrite(fn) {
  writeQueue = writeQueue.then(fn).catch(fn);
  return writeQueue;
}

function readData() {
  try {
    const raw = fs.readFileSync(DATA_FILE, 'utf8');
    return JSON.parse(raw);
  } catch {
    return { guests: [], tables: [] };
  }
}

function writeData(data) {
  return enqueueWrite(() => {
    const tmp = DATA_FILE + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf8');
    fs.renameSync(tmp, DATA_FILE);
  });
}

// Ensure data dir exists
try {
  fs.mkdirSync(path.dirname(DATA_FILE), { recursive: true });
} catch {}

// Ensure data file exists
if (!fs.existsSync(DATA_FILE)) {
  fs.writeFileSync(DATA_FILE, JSON.stringify({ guests: [], tables: [] }, null, 2), 'utf8');
}

// --- API Routes ---

// GET full data
app.get('/api/data', (req, res) => {
  res.json(readData());
});

// POST add guest
app.post('/api/guests', async (req, res) => {
  const { name } = req.body;
  if (!name || !name.trim()) {
    return res.status(400).json({ error: 'Name is required' });
  }
  const data = readData();
  const guest = {
    id: uuidv4(),
    name: name.trim(),
    tableId: null,
    seatLabel: null,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
  data.guests.push(guest);
  await writeData(data);
  res.status(201).json(guest);
});

// PUT update guest
app.put('/api/guests/:id', async (req, res) => {
  const data = readData();
  const guest = data.guests.find(g => g.id === req.params.id);
  if (!guest) return res.status(404).json({ error: 'Guest not found' });

  const { name, tableId, seatLabel } = req.body;
  if (name !== undefined) guest.name = name.trim();
  if (tableId !== undefined) guest.tableId = tableId;
  if (seatLabel !== undefined) guest.seatLabel = seatLabel;
  guest.updatedAt = new Date().toISOString();

  await writeData(data);
  res.json(guest);
});

// DELETE guest
app.delete('/api/guests/:id', async (req, res) => {
  const data = readData();
  const idx = data.guests.findIndex(g => g.id === req.params.id);
  if (idx === -1) return res.status(404).json({ error: 'Guest not found' });
  data.guests.splice(idx, 1);
  await writeData(data);
  res.status(204).end();
});

// POST assign guest to table
app.post('/api/guests/:id/assign', async (req, res) => {
  const { tableId, seatLabel } = req.body;
  if (!tableId) return res.status(400).json({ error: 'tableId is required' });

  const data = readData();
  const guest = data.guests.find(g => g.id === req.params.id);
  if (!guest) return res.status(404).json({ error: 'Guest not found' });

  const table = data.tables.find(t => t.id === tableId);
  if (!table) return res.status(404).json({ error: 'Table not found' });

  // Check seat conflict
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

  await writeData(data);
  res.json(guest);
});

// POST unassign guest from table
app.post('/api/guests/:id/unassign', async (req, res) => {
  const data = readData();
  const guest = data.guests.find(g => g.id === req.params.id);
  if (!guest) return res.status(404).json({ error: 'Guest not found' });

  guest.tableId = null;
  guest.seatLabel = null;
  guest.updatedAt = new Date().toISOString();

  await writeData(data);
  res.json(guest);
});

// POST add table
app.post('/api/tables', async (req, res) => {
  const data = readData();
  const maxNumber = data.tables.reduce((max, t) => Math.max(max, t.number), 0);
  const table = {
    id: uuidv4(),
    number: maxNumber + 1,
    label: (req.body.label || `${maxNumber + 1}桌`).trim(),
    createdAt: new Date().toISOString(),
  };
  data.tables.push(table);
  await writeData(data);
  res.status(201).json(table);
});

// PUT update table
app.put('/api/tables/:id', async (req, res) => {
  const data = readData();
  const table = data.tables.find(t => t.id === req.params.id);
  if (!table) return res.status(404).json({ error: 'Table not found' });

  const { number, label } = req.body;
  if (number !== undefined) table.number = number;
  if (label !== undefined) table.label = label.trim();

  await writeData(data);
  res.json(table);
});

// DELETE table (only if empty)
app.delete('/api/tables/:id', async (req, res) => {
  const data = readData();
  const idx = data.tables.findIndex(t => t.id === req.params.id);
  if (idx === -1) return res.status(404).json({ error: 'Table not found' });

  const hasGuests = data.guests.some(g => g.tableId === req.params.id);
  if (hasGuests) {
    return res.status(409).json({ error: 'Cannot delete table with assigned guests' });
  }

  data.tables.splice(idx, 1);
  await writeData(data);
  res.status(204).end();
});

app.listen(PORT, () => {
  console.log(`Wedding Seating server running on port ${PORT}`);
});
