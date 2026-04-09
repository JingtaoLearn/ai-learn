const fs = require("fs");
const path = require("path");

const DATA_FILE = process.env.DATA_FILE || "/data/projects.json";
const LOCK_FILE = DATA_FILE + ".lock";

function ensureDataFile() {
  const dir = path.dirname(DATA_FILE);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
  if (!fs.existsSync(DATA_FILE)) {
    fs.writeFileSync(DATA_FILE, JSON.stringify({}, null, 2));
  }
}

function readData() {
  ensureDataFile();
  const raw = fs.readFileSync(DATA_FILE, "utf-8");
  return JSON.parse(raw);
}

function writeData(data) {
  ensureDataFile();
  const tmp = DATA_FILE + ".tmp." + process.pid;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
  fs.renameSync(tmp, DATA_FILE);
}

// Simple file-based mutex — serializes all write operations
let queue = Promise.resolve();

function withLock(fn) {
  const next = queue.then(() => fn());
  queue = next.catch(() => {});
  return next;
}

function getProject(uuid) {
  const data = readData();
  const project = data[uuid];
  if (!project) return null;
  // Return a copy with soft-deleted items filtered out
  return {
    ...project,
    items: (project.items || []).filter((i) => !i.deleted),
  };
}

function createProject(name) {
  return withLock(() => {
    const data = readData();
    const { v4: uuidv4 } = require("uuid");
    const uuid = uuidv4();
    const now = new Date().toISOString();
    data[uuid] = {
      id: uuid,
      name,
      items: [],
      createdAt: now,
      updatedAt: now,
    };
    writeData(data);
    return data[uuid];
  });
}

function addItem(projectUuid, item) {
  return withLock(() => {
    const data = readData();
    const project = data[projectUuid];
    if (!project) return null;
    const now = new Date().toISOString();

    // Client provides id — upsert: if item with same id exists, update it instead
    const clientId = item.id;
    if (clientId) {
      const existing = project.items.find((i) => i.id === clientId && !i.deleted);
      if (existing) {
        // Upsert — treat as update
        const allowed = ["name", "quantity", "unit", "venue", "person", "status", "nextCheckDate", "notes"];
        for (const key of allowed) {
          if (item[key] !== undefined) existing[key] = item[key];
        }
        existing.updatedAt = now;
        project.updatedAt = now;
        writeData(data);
        return existing;
      }
    }

    const { v4: uuidv4 } = require("uuid");
    const newItem = {
      id: clientId || uuidv4(),
      name: item.name,
      quantity: item.quantity || 1,
      unit: item.unit || "件",
      venue: item.venue || "",
      person: item.person || "",
      status: item.status || "采买中",
      nextCheckDate: item.nextCheckDate || null,
      notes: item.notes || "",
      createdAt: now,
      updatedAt: now,
    };
    project.items.push(newItem);
    project.updatedAt = now;
    writeData(data);
    return newItem;
  });
}

function updateItem(projectUuid, itemId, updates) {
  return withLock(() => {
    const data = readData();
    const project = data[projectUuid];
    if (!project) return null;
    const idx = project.items.findIndex((i) => i.id === itemId && !i.deleted);
    if (idx === -1) return null;
    const now = new Date().toISOString();
    const item = project.items[idx];
    const allowed = [
      "name",
      "quantity",
      "unit",
      "venue",
      "person",
      "status",
      "nextCheckDate",
      "notes",
    ];
    for (const key of allowed) {
      if (updates[key] !== undefined) {
        item[key] = updates[key];
      }
    }
    item.updatedAt = now;
    project.updatedAt = now;
    writeData(data);
    return item;
  });
}

function deleteItem(projectUuid, itemId) {
  return withLock(() => {
    const data = readData();
    const project = data[projectUuid];
    if (!project) return false;
    const item = project.items.find((i) => i.id === itemId);
    if (!item) return false;
    const now = new Date().toISOString();
    item.deleted = true;
    item.deletedAt = now;
    item.updatedAt = now;
    project.updatedAt = now;
    writeData(data);
    return true;
  });
}

module.exports = { getProject, createProject, addItem, updateItem, deleteItem };
