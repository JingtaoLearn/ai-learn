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
  return data[uuid] || null;
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
    const { v4: uuidv4 } = require("uuid");
    const now = new Date().toISOString();
    const newItem = {
      id: uuidv4(),
      name: item.name,
      quantity: item.quantity || 1,
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
    const idx = project.items.findIndex((i) => i.id === itemId);
    if (idx === -1) return null;
    const now = new Date().toISOString();
    const item = project.items[idx];
    const allowed = [
      "name",
      "quantity",
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
    const idx = project.items.findIndex((i) => i.id === itemId);
    if (idx === -1) return false;
    project.items.splice(idx, 1);
    project.updatedAt = new Date().toISOString();
    writeData(data);
    return true;
  });
}

module.exports = { getProject, createProject, addItem, updateItem, deleteItem };
