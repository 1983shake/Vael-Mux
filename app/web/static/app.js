(() => {
    "use strict";

    const STAGE_LABELS = {
        web_ready: "Web 服务就绪",
        config_loading: "加载配置",
        fetching: "拉取订阅源",
        parsing: "解析节点",
        checking: "节点检测中",
        exporting: "生成订阅文件",
        idle: "空闲就绪",
        error: "发生错误",
    };

    const STAGE_COLORS = {
        web_ready: "#f5a623",
        config_loading: "#4a9eff",
        fetching: "#4a9eff",
        parsing: "#4a9eff",
        checking: "#8b5cf6",
        exporting: "#4a9eff",
        idle: "#22c55e",
        error: "#ef4444",
    };

    const $ = (id) => document.getElementById(id);

    let allNodes = [];
    let selectedIds = new Set();

    // ============================================================
    // 订阅链接
    // ============================================================
    function apiBase() {
        if (location.port === "8110") {
            return `${location.protocol}//${location.host}`;
        }
        return `${location.protocol}//${location.hostname}:8110`;
    }

    function setupApiLinks() {
        const base = apiBase();
        document.querySelectorAll("a.sub-link").forEach((a) => {
            const fmt = a.dataset.fmt;
            if (fmt) { a.href = `${base}/sub/${fmt}`; a.title = a.href; }
        });
    }

    // ============================================================
    // 状态渲染
    // ============================================================
    function renderState(s) {
        const color = STAGE_COLORS[s.stage] || "#4a9eff";
        $("stage-label").textContent = STAGE_LABELS[s.stage] || s.stage;
        $("message").textContent = s.message || "";
        $("stage-dot").style.background = color;

        const fill = $("progress-fill");
        fill.style.width = `${Math.max(0, Math.min(1, s.progress || 0)) * 100}%`;
        fill.style.background = color;

        $("subs-done").textContent = s.fetched_subscriptions ?? 0;
        $("subs-total").textContent = s.total_subscriptions ?? 0;
        $("nodes-checked").textContent = s.checked_nodes ?? 0;
        $("nodes-total").textContent = s.total_nodes ?? 0;
        $("nodes-alive").textContent = s.alive_nodes ?? 0;
        $("nodes-exported").textContent = s.exported_nodes ?? 0;

        const btn = $("btn-trigger");
        btn.disabled = !!s.running;
        btn.textContent = s.running ? "运行中..." : "立即执行一次";
    }

    // ============================================================
    // WebSocket
    // ============================================================
    let ws = null;
    let reconnectTimer = null;

    function connect() {
        const proto = location.protocol === "https:" ? "wss" : "ws";
        ws = new WebSocket(`${proto}://${location.host}/ws/status`);

        ws.onopen = () => { $("ws-state").textContent = "WebSocket 已连接"; };

        ws.onmessage = (e) => {
            let msg;
            try { msg = JSON.parse(e.data); } catch { return; }
            if (msg.event === "nodes_updated") {
                loadNodes();
            } else {
                renderState(msg);
                if (msg.stage === "idle") loadNodes();
            }
        };

        ws.onclose = () => {
            $("ws-state").textContent = "WebSocket 已断开，2 秒后重连...";
            clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connect, 2000);
        };

        ws.onerror = () => { try { ws.close(); } catch (_) { } };
    }

    // ============================================================
    // 节点加载 & 渲染
    // ============================================================
    async function loadNodes() {
        try {
            const resp = await fetch("/api/nodes");
            const data = await resp.json();
            allNodes = data.nodes || [];
            renderNodes();
        } catch (e) {
            console.error("加载节点失败", e);
        }
    }

    function latencyClass(ms) {
        if (ms == null) return "lat-na";
        if (ms < 200) return "lat-good";
        if (ms < 600) return "lat-mid";
        return "lat-bad";
    }

    function renderNodes() {
        const search = ($("node-search").value || "").trim().toLowerCase();
        const filter = $("node-filter").value;

        let list = allNodes.filter((n) => {
            if (filter === "enabled" && !n.enabled) return false;
            if (filter === "disabled" && n.enabled) return false;
            if (filter === "alive" && n.latency_ms == null) return false;
            if (filter === "dead" && n.latency_ms != null) return false;
            if (search) {
                const hay = `${n.name} ${n.server} ${n.type}`.toLowerCase();
                if (!hay.includes(search)) return false;
            }
            return true;
        });

        $("nodes-meta").textContent = `${list.length} / ${allNodes.length}`;
        const tbody = $("node-tbody");

        if (!list.length) {
            tbody.innerHTML = `<tr><td colspan="10" class="empty">暂无节点</td></tr>`;
            updateBatchButtons();
            return;
        }

        tbody.innerHTML = list.map((n) => {
            const latCls = latencyClass(n.latency_ms);
            const lat = n.latency_ms != null ? n.latency_ms : "—";
            const avg = n.latency_avg_ms != null ? n.latency_avg_ms : "—";
            const jit = n.jitter_ms != null ? n.jitter_ms : "—";
            const spd = n.speed_cps != null ? Number(n.speed_cps).toFixed(1) : "—";
            const sr = n.success_rate != null ? (n.success_rate * 100).toFixed(0) + "%" : "—";
            const typeBadge = `<span class="badge">${n.type}</span>`;
            const disabledBadge = n.enabled ? "" : ` <span class="badge disabled">已禁用</span>`;
            const checked = selectedIds.has(n.id) ? "checked" : "";

            return `
        <tr class="${n.enabled ? "" : "disabled"}">
          <td><input type="checkbox" data-id="${n.id}" class="row-check" ${checked}></td>
          <td class="name-cell" title="${escapeHtml(n.name)}">${escapeHtml(n.name || "—")}</td>
          <td>${typeBadge}</td>
          <td class="mono">${escapeHtml(n.server)}:${n.port}</td>
          <td class="${latCls}"><b>${lat}</b> <span class="lat-na">/${avg}</span></td>
          <td class="lat-na">${jit}</td>
          <td class="speed">${spd}</td>
          <td class="lat-na">${sr}</td>
          <td>${n.enabled ? "启用" : "禁用"}${disabledBadge}</td>
          <td class="col-actions">
            <div class="row-actions">
              <button data-action="edit" data-id="${n.id}">编辑</button>
              <button data-action="retest" data-id="${n.id}">重测</button>
              <button data-action="${n.enabled ? "disable" : "enable"}" data-id="${n.id}">
                ${n.enabled ? "禁用" : "启用"}
              </button>
              <button data-action="delete" data-id="${n.id}" class="danger">删除</button>
            </div>
          </td>
        </tr>
      `;
        }).join("");

        // 行内按钮
        tbody.querySelectorAll("button[data-action]").forEach((btn) => {
            btn.addEventListener("click", () => nodeAction(btn.dataset.action, btn.dataset.id));
        });

        // 复选框
        tbody.querySelectorAll(".row-check").forEach((cb) => {
            cb.addEventListener("change", () => {
                if (cb.checked) selectedIds.add(cb.dataset.id);
                else selectedIds.delete(cb.dataset.id);
                updateBatchButtons();
                syncCheckAll();
            });
        });

        syncCheckAll();
        updateBatchButtons();
    }

    function syncCheckAll() {
        const boxes = document.querySelectorAll(".row-check");
        const all = $("node-check-all");
        const checkedCount = document.querySelectorAll(".row-check:checked").length;
        all.checked = boxes.length > 0 && checkedCount === boxes.length;
        all.indeterminate = checkedCount > 0 && checkedCount < boxes.length;
    }

    function updateBatchButtons() {
        const n = selectedIds.size;
        ["btn-batch-enable", "btn-batch-disable", "btn-batch-retest", "btn-batch-delete"]
            .forEach((id) => { $(id).disabled = n === 0; });
    }

    function escapeHtml(s) {
        return String(s ?? "")
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    // ============================================================
    // 单节点操作
    // ============================================================
    async function nodeAction(action, id) {
        if (action === "edit") return openEditModal(id);

        if (action === "delete") {
            if (!confirm("确定删除该节点？")) return;
            await fetch(`/api/nodes/${id}`, { method: "DELETE" });
            selectedIds.delete(id);
            await loadNodes();
            return;
        }

        if (action === "retest") {
            const btn = document.activeElement;
            if (btn) btn.disabled = true;
            await fetch(`/api/nodes/${id}/retest`, { method: "POST" });
            await loadNodes();
            return;
        }

        if (action === "enable" || action === "disable") {
            await fetch(`/api/nodes/${id}/${action}`, { method: "POST" });
            await loadNodes();
        }
    }

    // ============================================================
    // 批量操作
    // ============================================================
    async function batchAction(action) {
        const ids = Array.from(selectedIds);
        if (!ids.length) return;
        if (action === "delete" && !confirm(`确定删除 ${ids.length} 个节点？`)) return;

        await fetch("/api/nodes/batch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ids, action }),
        });
        if (action === "delete") selectedIds.clear();
        await loadNodes();
    }

    // ============================================================
    // 弹窗：编辑 / 新增
    // ============================================================
    let modalMode = "edit";     // "edit" | "create"
    let editingId = null;

    function openCreateModal() {
        modalMode = "create";
        editingId = null;

        fillForm({
            type: "vmess",
            network: "tcp",
            port: 443,
            tls: true,
            enabled: true,
        });
        applyFormState();

        $("edit-modal-title").textContent = "新增节点";
        $("edit-modal-subtitle").textContent = "手动添加的节点将立即参与检测";
        $("btn-submit-edit").textContent = "创建并重测";

        $("edit-modal").classList.remove("hidden");
        setTimeout(() => document.querySelector('[name="name"]').focus(), 50);
    }

    function openEditModal(id) {
        const node = allNodes.find((n) => n.id === id);
        if (!node) return;

        modalMode = "edit";
        editingId = id;

        fillForm(node);
        applyFormState();

        $("edit-modal-title").textContent = "编辑节点";
        $("edit-modal-subtitle").textContent = node.id || "";
        $("btn-submit-edit").textContent = "保存并重测";

        $("edit-modal").classList.remove("hidden");
    }

    function closeEditModal() {
        $("edit-modal").classList.add("hidden");
        editingId = null;
    }

    function fillForm(node) {
        const form = $("edit-form");
        form.id.value = node.id || "";
        form.name.value = node.name || "";
        form.type.value = node.type || "vmess";
        form.server.value = node.server || "";
        form.port.value = node.port || "";
        form.uuid.value = node.uuid || "";
        form.password.value = node.password || "";
        form.cipher.value = node.cipher || "";
        form.alterId.value = node.alterId != null ? node.alterId : 0;
        form.flow.value = node.flow || "";
        form.network.value = node.network || "tcp";
        form.tls.checked = !!node.tls;
        form.sni.value = node.sni || "";
        form.host.value = node.host || "";
        form.path.value = node.path || "";
        form.skip_cert_verify.checked =
            !!(node.skip_cert_verify || node.insecure);
        form.enabled.checked = node.enabled !== false;
    }

    // 根据 协议 + 传输网络 动态显隐字段
    function applyFormState() {
        const form = $("edit-form");
        const type = form.type.value;
        const net = form.network.value;

        // 字段级显隐
        form.querySelectorAll("[data-types], [data-nets]").forEach((el) => {
            let show = true;
            if (el.dataset.types) {
                const types = el.dataset.types.split(",").map((s) => s.trim());
                show = show && types.includes(type);
            }
            if (el.dataset.nets) {
                const nets = el.dataset.nets.split(",").map((s) => s.trim());
                show = show && nets.includes(net);
            }
            el.classList.toggle("hidden", !show);
        });

        // 整个 section 若内部所有字段都隐藏，也一并隐藏
        form.querySelectorAll(".form-section").forEach((section) => {
            const items = section.querySelectorAll(".form-field, .checkbox-item");
            const anyVisible = Array.from(items).some(
                (el) => !el.classList.contains("hidden")
            );
            section.classList.toggle("hidden", !anyVisible);
        });
    }

    async function submitEdit(e) {
        e.preventDefault();
        const form = $("edit-form");
        const type = form.type.value;

        const payload = {
            name: form.name.value.trim(),
            type,
            server: form.server.value.trim(),
            port: Number(form.port.value),
            cipher: form.cipher.value.trim(),
            network: form.network.value,
            sni: form.sni.value.trim(),
            host: form.host.value.trim(),
            path: form.path.value.trim(),
            tls: form.tls.checked,
            skip_cert_verify: form.skip_cert_verify.checked,
            enabled: form.enabled.checked,
        };

        if (type === "vmess" || type === "vless") {
            payload.uuid = form.uuid.value.trim();
        }
        if (type === "vmess") {
            payload.alterId = Number(form.alterId.value) || 0;
        }
        if (type === "vless") {
            payload.flow = form.flow.value.trim();
        }
        if (type === "trojan" || type === "ss" || type === "hysteria2") {
            payload.password = form.password.value.trim();
        }

        // 前端校验
        if (!payload.server) {
            alert("请填写服务器地址");
            form.server.focus();
            return;
        }
        if (!payload.port || payload.port < 1 || payload.port > 65535) {
            alert("请填写有效端口（1 - 65535）");
            form.port.focus();
            return;
        }
        if ((type === "vmess" || type === "vless") && !payload.uuid) {
            alert("请填写 UUID");
            form.uuid.focus();
            return;
        }

        const submitBtn = $("btn-submit-edit");
        const originText = submitBtn.textContent;
        submitBtn.disabled = true;
        submitBtn.textContent = "提交中...";

        try {
            let resp;
            if (modalMode === "create") {
                resp = await fetch("/api/nodes", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
            } else {
                resp = await fetch(`/api/nodes/${editingId}`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
            }

            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                alert(data.detail || `保存失败 (${resp.status})`);
                return;
            }
            closeEditModal();
            await loadNodes();
        } catch (err) {
            alert("请求失败：" + err.message);
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = originText;
        }
    }

    // ============================================================
    // 手动触发
    // ============================================================
    async function trigger() {
        const hint = $("action-hint");
        hint.textContent = "";
        try {
            const resp = await fetch("/api/trigger", { method: "POST" });
            const data = await resp.json().catch(() => ({}));
            if (resp.ok && data.ok) {
                hint.textContent = data.message || "已触发";
                hint.style.color = "#22c55e";
            } else {
                hint.textContent = data.message || `触发失败 (${resp.status})`;
                hint.style.color = "#ef4444";
            }
        } catch (err) {
            hint.textContent = "请求失败：" + err.message;
            hint.style.color = "#ef4444";
        }
    }

    // ============================================================
    // 事件绑定
    // ============================================================
    function bind() {
        $("btn-trigger").addEventListener("click", trigger);

        $("btn-refresh-nodes").addEventListener("click", loadNodes);
        $("btn-add-node").addEventListener("click", openCreateModal);
        $("node-search").addEventListener("input", renderNodes);
        $("node-filter").addEventListener("change", renderNodes);

        $("node-check-all").addEventListener("change", (e) => {
            const checked = e.target.checked;
            document.querySelectorAll(".row-check").forEach((cb) => {
                cb.checked = checked;
                if (checked) selectedIds.add(cb.dataset.id);
                else selectedIds.delete(cb.dataset.id);
            });
            updateBatchButtons();
        });

        $("btn-batch-enable").addEventListener("click", () => batchAction("enable"));
        $("btn-batch-disable").addEventListener("click", () => batchAction("disable"));
        $("btn-batch-retest").addEventListener("click", () => batchAction("retest"));
        $("btn-batch-delete").addEventListener("click", () => batchAction("delete"));

        // 弹窗
        $("btn-close-edit").addEventListener("click", closeEditModal);
        $("btn-cancel-edit").addEventListener("click", closeEditModal);
        $("edit-form").addEventListener("submit", submitEdit);

        // 联动显隐
        $("edit-form").type.addEventListener("change", applyFormState);
        $("edit-form").network.addEventListener("change", applyFormState);

        $("edit-modal").addEventListener("click", (e) => {
            if (e.target.id === "edit-modal") closeEditModal();
        });

        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") closeEditModal();
        });
    }

    // ============================================================
    // 启动
    // ============================================================
    setupApiLinks();
    bind();
    connect();
    loadNodes();
})();