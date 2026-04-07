(function () {
  "use strict";

  const VENUES = ["丰县", "婚房", "婚礼现场", "宴会厅", "埠口家"];
  const PERSONS = ["张景涛", "渠琪", "丛领兹"];
  const STATUSES = ["采买中", "已收货", "已取货", "已就绪"];
  const UNITS = ["件", "每人件", "斤"];

  const app = document.getElementById("app");
  let state = { page: "loading", project: null, filters: {} };

  // --- Routing ---
  function getRoute() {
    const path = window.location.pathname;
    const match = path.match(/^\/p\/([0-9a-f-]{36})$/);
    return match ? { page: "project", uuid: match[1] } : { page: "landing" };
  }

  function navigate(url) {
    history.pushState(null, "", url);
    init();
  }

  window.addEventListener("popstate", init);

  // --- API ---
  async function api(method, path, body) {
    const opts = {
      method,
      headers: { "Content-Type": "application/json" },
    };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch("/api" + path, opts);
    if (res.status === 204) return null;
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "请求失败");
    return data;
  }

  // --- Render helpers ---
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (k.startsWith("on") && typeof v === "function") {
          el.addEventListener(k.slice(2).toLowerCase(), v);
        } else if (k === "className") {
          el.className = v;
        } else if (k === "htmlFor") {
          el.setAttribute("for", v);
        } else {
          el.setAttribute(k, v);
        }
      }
    }
    for (const child of children.flat()) {
      if (child == null) continue;
      el.appendChild(
        typeof child === "string" ? document.createTextNode(child) : child,
      );
    }
    return el;
  }

  function clear() {
    app.innerHTML = "";
  }

  // --- Landing page ---
  function renderLanding() {
    clear();
    const container = h("div", { className: "landing" },
      h("div", null,
        h("h1", null, "婚礼筹备清单"),
        h("p", null, "轻松管理婚礼物品准备"),
      ),
      h("button", {
        className: "btn btn-primary",
        onClick: () => showCreateDialog(),
      }, "+ 创建新项目"),
    );
    app.appendChild(container);
  }

  function showCreateDialog() {
    clear();
    const container = h("div", { className: "landing" },
      h("div", { className: "create-dialog" },
        h("h2", null, "创建新项目"),
        h("div", { className: "form-group" },
          h("label", null, "项目名称"),
          h("input", {
            type: "text",
            id: "project-name",
            placeholder: "例如：婚礼筹备",
            value: "婚礼筹备",
          }),
        ),
        h("div", { className: "form-actions" },
          h("button", {
            className: "btn btn-secondary",
            onClick: () => renderLanding(),
          }, "取消"),
          h("button", {
            className: "btn btn-primary",
            onClick: handleCreate,
          }, "创建"),
        ),
      ),
    );
    app.appendChild(container);
    document.getElementById("project-name").select();
  }

  async function handleCreate() {
    const input = document.getElementById("project-name");
    const name = input.value.trim();
    if (!name) return;
    try {
      const project = await api("POST", "/projects", { name });
      navigate("/p/" + project.id);
    } catch (err) {
      alert(err.message);
    }
  }

  // --- Project page ---
  async function loadProject(uuid) {
    clear();
    app.appendChild(h("div", { className: "loading" }, "加载中..."));
    try {
      const project = await api("GET", "/projects/" + uuid);
      state.project = project;
      state.filters = {};
      renderProject();
    } catch (err) {
      clear();
      app.appendChild(
        h("div", { className: "empty" },
          h("p", null, "项目不存在或链接无效"),
          h("button", {
            className: "btn btn-primary",
            style: "margin-top:16px",
            onClick: () => navigate("/"),
          }, "返回首页"),
        ),
      );
    }
  }

  function getFilteredItems() {
    let items = state.project.items || [];
    const f = state.filters;
    if (f.venue) items = items.filter((i) => i.venue === f.venue);
    if (f.person) items = items.filter((i) => i.person === f.person);
    if (f.status) items = items.filter((i) => i.status === f.status);
    return items;
  }

  function renderProject() {
    clear();
    const p = state.project;
    const items = getFilteredItems();

    // Header
    app.appendChild(
      h("div", { className: "project-header" },
        h("h1", null, p.name),
        h("div", { className: "meta" }, `共 ${p.items.length} 项`),
      ),
    );

    // Share bar
    const shareUrl = window.location.origin + "/p/" + p.id;
    const shareInput = h("input", { type: "text", value: shareUrl, readOnly: "true" });
    app.appendChild(
      h("div", { className: "share-bar" },
        shareInput,
        h("button", {
          className: "btn btn-secondary btn-sm",
          onClick: () => {
            shareInput.select();
            navigator.clipboard.writeText(shareUrl);
          },
        }, "复制"),
      ),
    );

    // Summary bar
    const statusCounts = {};
    for (const s of STATUSES) statusCounts[s] = 0;
    for (const item of p.items) {
      if (statusCounts[item.status] !== undefined) statusCounts[item.status]++;
    }
    app.appendChild(
      h("div", { className: "summary-bar" },
        ...STATUSES.map((s) =>
          h("span", { className: `summary-chip status-${s}` }, `${s} ${statusCounts[s]}`),
        ),
      ),
    );

    // Filters
    app.appendChild(
      h("div", { className: "filters" },
        makeSelect("所有场地", VENUES, state.filters.venue, (v) => {
          state.filters.venue = v;
          renderProject();
        }),
        makeSelect("所有负责人", PERSONS, state.filters.person, (v) => {
          state.filters.person = v;
          renderProject();
        }),
        makeSelect("所有状态", STATUSES, state.filters.status, (v) => {
          state.filters.status = v;
          renderProject();
        }),
      ),
    );

    // Add button
    app.appendChild(
      h("div", { className: "toolbar" },
        h("button", {
          className: "btn btn-primary btn-block",
          onClick: () => showItemModal(),
        }, "+ 添加物品"),
      ),
    );

    // Item list
    if (items.length === 0) {
      app.appendChild(
        h("div", { className: "empty" },
          h("p", null, state.project.items.length === 0 ? "还没有物品，点击上方按钮添加" : "没有匹配的物品"),
        ),
      );
    } else {
      const list = h("div", { className: "item-list" });
      for (const item of items) {
        list.appendChild(renderItemCard(item));
      }
      app.appendChild(list);
    }
  }

  function makeSelect(label, options, current, onChange) {
    const sel = h("select", {
      className: "filter-select",
      onChange: (e) => onChange(e.target.value || ""),
    },
      h("option", { value: "" }, label),
      ...options.map((o) => {
        const opt = h("option", { value: o }, o);
        if (current === o) opt.selected = true;
        return opt;
      }),
    );
    return sel;
  }

  function renderItemCard(item) {
    const tags = [
      item.venue ? h("span", { className: "tag tag-venue" }, item.venue) : null,
      item.person ? h("span", { className: "tag tag-person" }, item.person) : null,
      h("span", { className: `tag tag-status status-${item.status}` }, item.status),
      item.quantity ? h("span", { className: "tag tag-qty" }, `x${item.quantity}${item.unit || "件"}`) : null,
    ].filter(Boolean);

    const card = h("div", {
      className: "item-card",
      onClick: () => showItemModal(item),
    },
      h("div", { className: "item-name" }, item.name),
      h("div", { className: "item-details" }, ...tags),
      item.notes ? h("div", { className: "item-notes" }, item.notes) : null,
      item.nextCheckDate ? h("div", { className: "item-date" }, `下次关注: ${item.nextCheckDate}`) : null,
    );
    return card;
  }

  // --- Item modal ---
  let autoSaveTimer = null;
  let savingIndicator = null;

  function showItemModal(item) {
    const isEdit = !!item;
    const overlay = h("div", { className: "modal-overlay", onClick: (e) => {
      if (e.target === overlay) {
        if (isEdit) { flushAutoSave(overlay, item); }
        overlay.remove();
      }
    }});

    const fields = {
      name: item?.name || "",
      quantity: item?.quantity || 1,
      unit: item?.unit || "件",
      venue: item?.venue || "",
      person: item?.person || "",
      status: item?.status || "采买中",
      nextCheckDate: item?.nextCheckDate || "",
      notes: item?.notes || "",
    };

    // Auto-save handler for edit mode — debounced, fires on every field change
    function onFieldChange() {
      if (!isEdit) return;
      if (autoSaveTimer) clearTimeout(autoSaveTimer);
      if (savingIndicator) savingIndicator.textContent = "正在保存...";
      autoSaveTimer = setTimeout(() => doAutoSave(overlay, item), 500);
    }

    savingIndicator = isEdit ? h("span", { className: "save-indicator" }, "") : null;

    const modal = h("div", { className: "modal" },
      h("div", { className: "modal-title-row" },
        h("h2", null, isEdit ? "编辑物品" : "添加物品"),
        savingIndicator,
      ),

      h("div", { className: "form-group" },
        h("label", null, "名称"),
        h("input", { type: "text", id: "f-name", value: fields.name, placeholder: "物品名称",
          onInput: onFieldChange }),
      ),
      h("div", { className: "form-group" },
        h("label", null, "数量"),
        h("div", { className: "qty-row" },
          h("input", { type: "number", id: "f-quantity", value: String(fields.quantity), min: "1",
            onInput: onFieldChange }),
          makeFormSelect("f-unit", UNITS, fields.unit, "", onFieldChange),
        ),
      ),
      h("div", { className: "form-group" },
        h("label", null, "使用场地"),
        makeFormSelect("f-venue", VENUES, fields.venue, "选择场地", onFieldChange),
      ),
      h("div", { className: "form-group" },
        h("label", null, "负责人"),
        makeFormSelect("f-person", PERSONS, fields.person, "选择负责人", onFieldChange),
      ),
      h("div", { className: "form-group" },
        h("label", null, "状态"),
        makeFormSelect("f-status", STATUSES, fields.status, "", onFieldChange),
      ),
      h("div", { className: "form-group" },
        h("label", null, "下次关注日"),
        h("input", { type: "date", id: "f-nextCheckDate", value: fields.nextCheckDate,
          onChange: onFieldChange }),
      ),
      h("div", { className: "form-group" },
        h("label", null, "备注"),
        h("textarea", { id: "f-notes", placeholder: "可选备注", onInput: onFieldChange }, fields.notes),
      ),

      isEdit
        ? h("div", { className: "form-actions" },
            h("button", {
              className: "btn btn-secondary btn-block",
              onClick: () => { flushAutoSave(overlay, item); overlay.remove(); },
            }, "关闭"),
          )
        : h("div", { className: "form-actions" },
            h("button", {
              className: "btn btn-secondary",
              onClick: () => overlay.remove(),
            }, "取消"),
            h("button", {
              className: "btn btn-primary",
              onClick: () => handleSaveItem(overlay, item),
            }, "添加"),
          ),

      isEdit ? h("div", { className: "delete-row" },
        h("button", {
          className: "btn btn-danger btn-block btn-sm",
          onClick: () => handleDeleteItem(overlay, item),
        }, "删除此物品"),
      ) : null,
    );

    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    // Focus name input
    setTimeout(() => document.getElementById("f-name")?.focus(), 100);
  }

  async function doAutoSave(overlay, item) {
    const data = getFormData();
    if (!data.name) return;
    try {
      await api("PUT", `/projects/${state.project.id}/items/${item.id}`, data);
      if (savingIndicator) savingIndicator.textContent = "✓ 已保存";
      // Update local state
      const idx = state.project.items.findIndex((i) => i.id === item.id);
      if (idx !== -1) Object.assign(state.project.items[idx], data);
    } catch (err) {
      if (savingIndicator) savingIndicator.textContent = "保存失败";
    }
  }

  function flushAutoSave(overlay, item) {
    if (autoSaveTimer) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
      doAutoSave(overlay, item);
    }
  }

  function makeFormSelect(id, options, current, placeholder, onChange) {
    const attrs = { id };
    if (onChange) attrs.onChange = onChange;
    const sel = h("select", attrs,
      placeholder ? h("option", { value: "" }, placeholder) : null,
      ...options.map((o) => {
        const opt = h("option", { value: o }, o);
        if (current === o) opt.selected = true;
        return opt;
      }),
    );
    return sel;
  }

  function getFormData() {
    return {
      name: document.getElementById("f-name").value.trim(),
      quantity: parseInt(document.getElementById("f-quantity").value, 10) || 1,
      unit: document.getElementById("f-unit").value || "件",
      venue: document.getElementById("f-venue").value,
      person: document.getElementById("f-person").value,
      status: document.getElementById("f-status").value,
      nextCheckDate: document.getElementById("f-nextCheckDate").value || null,
      notes: document.getElementById("f-notes").value.trim(),
    };
  }

  async function handleSaveItem(overlay, existing) {
    const data = getFormData();
    if (!data.name) {
      alert("请输入物品名称");
      return;
    }
    try {
      if (existing) {
        await api("PUT", `/projects/${state.project.id}/items/${existing.id}`, data);
      } else {
        await api("POST", `/projects/${state.project.id}/items`, data);
      }
      overlay.remove();
      await refreshProject();
    } catch (err) {
      alert(err.message);
    }
  }

  async function handleDeleteItem(overlay, item) {
    if (!confirm("确定删除「" + item.name + "」？")) return;
    try {
      await api("DELETE", `/projects/${state.project.id}/items/${item.id}`);
      overlay.remove();
      await refreshProject();
    } catch (err) {
      alert(err.message);
    }
  }

  async function refreshProject() {
    try {
      state.project = await api("GET", "/projects/" + state.project.id);
      renderProject();
    } catch (err) {
      alert("刷新失败: " + err.message);
    }
  }

  // --- Init ---
  function init() {
    const route = getRoute();
    if (route.page === "project") {
      loadProject(route.uuid);
    } else {
      renderLanding();
    }
  }

  init();
})();
