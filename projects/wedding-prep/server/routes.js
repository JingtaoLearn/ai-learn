const express = require("express");
const storage = require("./storage");

const router = express.Router();

// Create a new project
router.post("/projects", async (req, res) => {
  const { name } = req.body;
  if (!name || !name.trim()) {
    return res.status(400).json({ error: "项目名称不能为空" });
  }
  try {
    const project = await storage.createProject(name.trim());
    res.status(201).json(project);
  } catch (err) {
    console.error("Create project error:", err);
    res.status(500).json({ error: "创建项目失败" });
  }
});

// Get project by UUID
router.get("/projects/:uuid", (req, res) => {
  try {
    const project = storage.getProject(req.params.uuid);
    if (!project) {
      return res.status(404).json({ error: "项目不存在" });
    }
    res.json(project);
  } catch (err) {
    console.error("Get project error:", err);
    res.status(500).json({ error: "获取项目失败" });
  }
});

// Add item to project
router.post("/projects/:uuid/items", async (req, res) => {
  try {
    const item = await storage.addItem(req.params.uuid, req.body);
    if (!item) {
      return res.status(404).json({ error: "项目不存在" });
    }
    res.status(201).json(item);
  } catch (err) {
    console.error("Add item error:", err);
    res.status(500).json({ error: "添加物品失败" });
  }
});

// Update item
router.put("/projects/:uuid/items/:itemId", async (req, res) => {
  try {
    const item = await storage.updateItem(
      req.params.uuid,
      req.params.itemId,
      req.body,
    );
    if (!item) {
      return res.status(404).json({ error: "物品不存在" });
    }
    res.json(item);
  } catch (err) {
    console.error("Update item error:", err);
    res.status(500).json({ error: "更新物品失败" });
  }
});

// Delete item
router.delete("/projects/:uuid/items/:itemId", async (req, res) => {
  try {
    const ok = await storage.deleteItem(req.params.uuid, req.params.itemId);
    if (!ok) {
      return res.status(404).json({ error: "物品不存在" });
    }
    res.status(204).end();
  } catch (err) {
    console.error("Delete item error:", err);
    res.status(500).json({ error: "删除物品失败" });
  }
});

module.exports = router;
