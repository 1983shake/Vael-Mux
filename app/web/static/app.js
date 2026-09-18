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
        stopped: "已停止",
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
        stopped: "#f5a623",
    };

    const LATENCY_COLOR = "#4a9eff";
    const SPEED_COLOR = "#8b5cf6";

    const PAGE_SIZE_KEY = "vael-mux-page-size";
    const PAGE_SIZE_OPTIONS = [5, 10, 20, 50, 100, 200];
    const DEFAULT_PAGE_SIZE = 20;

    const LOG_MAX_LINES = 500;
    const LOG_AUTOSCROLL_THRESHOLD = 40;

    const $ = (id) => document.getElementById(id);

    let latencyTargets = [];
    let speedTargets = [];

    let currentPage = 1;
    let pageSize = loadPageSize();
    let totalNodes = 0;
    let totalPages = 1;
    let currentPageIds = [];
    let selectedIds = new Set();

    let modalMode = "edit";
    let editingId = null;

    let logAutoScroll = true;

    // ============================================================
    // 分页尺寸记忆
    // ============================================================
    function loadPageSize() {
        try {
            const s = parseInt(localStorage.getItem(PAGE_SIZE_KEY) || "", 10);
            if (PAGE_SIZE_OPTIONS.includes(s)) return s;
        } catch (_) { }
        return DEFAULT_PAGE_SIZE;
    }

    function savePageSize(size) {
        try { localStorage.setItem(PAGE_SIZE_KEY, String(size)); } catch (_) { }
    }

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
    // 日志
    // ============================================================
    function initLogView() {
        const view = $("log-view");
        if (!view) return;

        view.addEventListener("scroll", () => {
            const dist = view.scrollHeight - view.clientHeight - view.scrollTop;
            logAutoScroll = dist < LOG_AUTOSCROLL_THRESHOLD;
        });

        const clearBtn = $("btn-clear-log");
        if (clearBtn) {
            clearBtn.addEventListener("click", () => {
                view.innerHTML = "";
                logAutoScroll = true;
            });
        }
    }

    function appendLog(entry) {
        const view = $("log-view");
        if (!view) return;

        const level = String(entry.level || "info").toLowerCase();
        const div = document.createElement("div");
        div.className = "log-line log-" + level;
        div.textContent = entry.line || "";

        view.appendChild(div);

        while (view.childElementCount > LOG_MAX_LINES) {
            view.removeChild(view.firstChild);
        }

        if (logAutoScroll) {
            view.scrollTop = view.scrollHeight;
        }
    }

    // ============================================================
    // 状态渲染
    // ============================================================
    function renderState(s) {
        const running = !!s.running;
        const phase = (s.phase || "").toLowerCase();
        const stage = s.stage || "";

        let stageLabel = STAGE_LABELS[stage] || stage;
        let stageColor = STAGE_COLORS[stage] || LATENCY_COLOR;

        if (stage === "checking") {
            if (phase === "latency") {
                stageLabel = "延迟 -> 速度（单节点流水线）";
                stageColor = LATENCY_COLOR;
            } else if (phase === "speed") {
                stageLabel = "延迟 -> 速度（单节点流水线）";
                stageColor = SPEED_COLOR;
            }
        }

        $("stage-label").textContent = stageLabel;
        $("stage-dot").style.background = stageColor;

        // 消息
        $("message").textContent = s.message || "";

        // 主进度条
        const fill = $("progress-fill");
        fill.style.width = `${Math.max(0, Math.min(1, s.progress || 0)) * 100}%`;
        fill.style.background = stageColor;

        // 六项统计
        $("subs-done").textContent = s.fetched_subscriptions ?? 0;
        $("subs-total").textContent = s.total_subscriptions ?? 0;

        $("nodes-checked").textContent = s.checked_nodes ?? 0;
        $("nodes-total").textContent = s.total_nodes ?? 0;

        // 有效延迟
        $("nodes-alive").textContent = s.alive_nodes ?? 0;

        // 有效速度
        $("speed-passed").textContent = s.speed_passed ?? 0;

        // 有效节点：有效数量 / 上限（未设置显示 ∞）
        $("valid-nodes").textContent = s.speed_passed ?? 0;
        const maxValid = s.max_valid_nodes || 0;
        $("max-valid").textContent = maxValid > 0 ? maxValid : "∞";

        $("nodes-exported").textContent = s.exported_nodes ?? 0;

        // 速度阶段专属进度条
        const speedWrap = $("speed-progress-wrap");
        const spdTotal = s.speed_total || 0;
        const spdDone = s.speed_checked || 0;
        const spdPassed = s.speed_passed || 0;
        if (stage === "checking" && phase === "speed" && spdTotal > 0) {
            speedWrap.classList.remove("hidden");
            const pct = Math.min(1, spdDone / spdTotal);
            $("speed-progress-fill").style.width = `${pct * 100}%`;
            $("speed-progress-text").textContent =
                `${spdDone} / ${spdTotal} · 有效 ${spdPassed}`;
        } else {
            speedWrap.classList.add("hidden");
        }

        // 按钮
        const triggerBtn = $("btn-trigger");
        const stopBtn = $("btn-stop");
        triggerBtn.disabled = running;
        triggerBtn.textContent = running ? "运行中..." : "重新更新节点";
        stopBtn.disabled = !running;
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

            if (msg.event === "log") {
                appendLog(msg);
                return;
            }

            if (msg.event === "nodes_updated") {
                loadNodes();
                return;
            }

            renderState(msg);
            if (msg.stage === "idle" || msg.stage === "stopped") loadNodes();
        };

        ws.onclose = () => {
            $("ws-state").textContent = "WebSocket 已断开，2 秒后重连...";
            clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connect, 2000);
        };

        ws.onerror = () => { try { ws.close(); } catch (_) { } };
    }

    // ============================================================
    // 表格列头（动态，含启用/禁用状态）
    // ============================================================
    function targetsChanged(a, b) {
        if (a.length !== b.length) return true;
        for (let i = 0; i < a.length; i++) {
            if (a[i].name !== b[i].name) return true;
            if (a[i].url !== b[i].url) return true;
            if (!!a[i].enabled !== !!b[i].enabled) return true;
        }
        return false;
    }

    function buildTableHeader() {
        const thead = $("node-thead");

        const latCols = latencyTargets.map((t) => {
            const off = t.enabled === false;
            const cls = off ? "target-col target-latency target-disabled" : "target-col target-latency";
            const tag = off ? "已关闭" : "延迟";
            const title = (t.url || "") + (off ? "（已关闭）" : "");
            return `
                <th class="${cls}" title="${escapeHtml(title)}">
                    ${escapeHtml(t.name)}
                    <span class="target-tag">${tag}</span>
                </th>
            `;
        }).join("");

        const spdCols = speedTargets.map((t) => {
            const off = t.enabled === false;
            const cls = off ? "target-col target-speed target-disabled" : "target-col target-speed";
            const tag = off ? "已关闭" : "速度";
            const title = (t.url || "") + (off ? "（已关闭）" : "");
            return `
                <th class="${cls}" title="${escapeHtml(title)}">
                    ${escapeHtml(t.name)}
                    <span class="target-tag">${tag}</span>
                </th>
            `;
        }).join("");

        thead.innerHTML = `
            <tr>
                <th class="col-check"><input type="checkbox" id="node-check-all"></th>
                <th>名称</th>
                <th>类型</th>
                <th>服务器</th>
                ${latCols}
                ${spdCols}
            </tr>
        `;
    }

    // ============================================================
    // 节点加载（后端分页）
    // ============================================================
    async function loadNodes() {
        try {
            const params = new URLSearchParams({
                page: String(currentPage),
                page_size: String(pageSize),
                search: $("node-search").value.trim(),
                filter: $("node-filter").value,
            });
            const resp = await fetch(`/api/nodes?${params}`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();

            const newLat = data.latency_targets || [];
            const newSpd = data.speed_targets || [];
            const needRebuild = targetsChanged(newLat, latencyTargets)
                || targetsChanged(newSpd, speedTargets);

            latencyTargets = newLat;
            speedTargets = newSpd;
            if (needRebuild) buildTableHeader();

            totalNodes = data.total || 0;
            totalPages = data.pages || 1;
            currentPage = data.page || 1;
            pageSize = data.page_size || pageSize;

            renderNodes(data.nodes || []);
            updatePagination();
        } catch (e) {
            console.error("加载节点失败", e);
            $("nodes-meta").textContent = "加载失败";
        }
    }

    function latencyClass(ms) {
        if (ms == null) return "lat-na";
        if (ms < 200) return "lat-good";
        if (ms < 600) return "lat-mid";
        return "lat-bad";
    }

    function speedClass(mbps) {
        const v = Number(mbps);
        if (!isFinite(v) || v <= 0) return "lat-na";
        if (v >= 10) return "lat-good";
        if (v >= 3) return "lat-mid";
        return "lat-bad";
    }

    /**
     * 速度格式化：
     *   < 0.1 Mbps  -> 保留 3 位小数（如 0.063）
     *   < 1 Mbps    -> 保留 2 位小数（如 0.56）
     *   >= 1 Mbps   -> 保留 2 位小数（如 12.34）
     *   无效（null / <= 0 / NaN） -> null
     */
    function formatSpeed(mbps) {
        const v = Number(mbps);
        if (!isFinite(v) || v <= 0) return null;
        if (v < 0.1) return v.toFixed(3);
        return v.toFixed(2);
    }

    function renderNodes(nodes) {
        currentPageIds = nodes.map((n) => n.id);
        const tbody = $("node-tbody");
        const colCount = 4 + latencyTargets.length + speedTargets.length;

        if (!nodes.length) {
            tbody.innerHTML = `<tr><td colspan="${colCount}" class="empty">暂无节点</td></tr>`;
            updateBatchButtons();
            syncCheckAll();
            return;
        }

        tbody.innerHTML = nodes.map((n) => {
            const checked = selectedIds.has(n.id) ? "checked" : "";

            const latCells = latencyTargets.map((t) => {
                if (t.enabled === false) {
                    return `<td class="lat-disabled">×</td>`;
                }
                const r = n.targets && n.targets.latency ? n.targets.latency[t.name] : null;
                if (!r || r.latency_ms == null) return `<td class="lat-na">—</td>`;
                return `<td class="${latencyClass(r.latency_ms)}">${r.latency_ms}</td>`;
            }).join("");

            const spdCells = speedTargets.map((t) => {
                if (t.enabled === false) {
                    return `<td class="lat-disabled">×</td>`;
                }
                const r = n.targets && n.targets.speed ? n.targets.speed[t.name] : null;
                const v = r ? r.speed_mbps : null;
                const text = formatSpeed(v);
                if (text == null) return `<td class="lat-na">—</td>`;
                return `<td class="${speedClass(v)}">${text}</td>`;
            }).join("");

            return `
                <tr class="${n.enabled ? "" : "disabled"}">
                    <td><input type="checkbox" data-id="${n.id}" class="row-check" ${checked}></td>
                    <td class="name-cell">
                        <a href="#" class="node-name" data-id="${n.id}" title="${escapeHtml(n.name)}">${escapeHtml(n.name || "—")}</a>
                    </td>
                    <td><span class="badge">${escapeHtml(n.type)}</span></td>
                    <td class="mono">${escapeHtml(n.server)}:${n.port}</td>
                    ${latCells}
                    ${spdCells}
                </tr>
            `;
        }).join("");

        tbody.querySelectorAll(".node-name").forEach((a) => {
            a.addEventListener("click", (e) => {
                e.preventDefault();
                openEditModal(a.dataset.id);
            });
        });

        tbody.querySelectorAll(".row-check").forEach((cb) => {
            cb.addEventListener("change", () => {
                if (cb.checked) selectedIds.add(cb.dataset.id);
                else selectedIds.delete(cb.dataset.id);
                updateBatchButtons();
                syncCheckAll();
            });
        });

        updateBatchButtons();
        syncCheckAll();
    }

    function syncCheckAll() {
        const all = $("node-check-all");
        if (!all) return;
        const boxes = document.querySelectorAll(".row-check");
        if (!boxes.length) {
            all.checked = false;
            all.indeterminate = false;
            return;
        }
        const checked = document.querySelectorAll(".row-check:checked").length;
        all.checked = checked === boxes.length;
        all.indeterminate = checked > 0 && checked < boxes.length;
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
    // 分页控件
    // ============================================================
    function updatePagination() {
        $("page-total").textContent = totalNodes;
        $("page-current").textContent = currentPage;
        $("page-count").textContent = totalPages;

        $("page-first").disabled = currentPage <= 1;
        $("page-prev").disabled = currentPage <= 1;
        $("page-next").disabled = currentPage >= totalPages;
        $("page-last").disabled = currentPage >= totalPages;

        $("nodes-meta").textContent = `${totalNodes} 条`;
    }

    function gotoPage(p) {
        if (p < 1 || p > totalPages || p === currentPage) return;
        currentPage = p;
        loadNodes();
    }

    // ============================================================
    // 单节点操作
    // ============================================================
    async function nodeAction(action, id) {
        if (action === "delete") {
            if (!confirm("确定删除该节点？")) return;
            await fetch(`/api/nodes/${id}`, { method: "DELETE" });
            selectedIds.delete(id);
            await loadNodes();
            return;
        }
        if (action === "retest") {
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
        $("btn-delete-node").style.display = "none";

        $("edit-modal").classList.remove("hidden");
        setTimeout(() => document.querySelector('[name="name"]').focus(), 50);
    }

    async function openEditModal(id) {
        try {
            const resp = await fetch(`/api/nodes/${id}`);
            if (!resp.ok) { alert("节点不存在"); return; }
            const node = await resp.json();

            modalMode = "edit";
            editingId = id;

            fillForm(node);
            applyFormState();

            $("edit-modal-title").textContent = "编辑节点";
            $("edit-modal-subtitle").textContent = node.id || "";
            $("btn-submit-edit").textContent = "保存并重测";
            $("btn-delete-node").style.display = "";

            $("edit-modal").classList.remove("hidden");
        } catch (e) {
            alert("加载节点失败：" + e.message);
        }
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
        form.skip_cert_verify.checked = !!(node.skip_cert_verify || node.insecure);
        form.enabled.checked = node.enabled !== false;
    }

    function applyFormState() {
        const form = $("edit-form");
        const type = form.type.value;
        const net = form.network.value;

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

        if (type === "vmess" || type === "vless") payload.uuid = form.uuid.value.trim();
        if (type === "vmess") payload.alterId = Number(form.alterId.value) || 0;
        if (type === "vless") payload.flow = form.flow.value.trim();
        if (type === "trojan" || type === "ss" || type === "hysteria2") {
            payload.password = form.password.value.trim();
        }

        if (!payload.server) { alert("请填写服务器地址"); form.server.focus(); return; }
        if (!payload.port || payload.port < 1 || payload.port > 65535) {
            alert("请填写有效端口（1 - 65535）"); form.port.focus(); return;
        }
        if ((type === "vmess" || type === "vless") && !payload.uuid) {
            alert("请填写 UUID"); form.uuid.focus(); return;
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

    async function deleteFromModal() {
        if (!editingId) return;
        if (!confirm("确定删除该节点？此操作不可撤销。")) return;
        try {
            const resp = await fetch(`/api/nodes/${editingId}`, { method: "DELETE" });
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                alert(data.detail || `删除失败 (${resp.status})`);
                return;
            }
            selectedIds.delete(editingId);
            closeEditModal();
            await loadNodes();
        } catch (e) {
            alert("请求失败：" + e.message);
        }
    }

    // ============================================================
    // 触发 / 停止
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
                hint.textContent = data.message || data.detail || `触发失败 (${resp.status})`;
                hint.style.color = "#ef4444";
            }
        } catch (err) {
            hint.textContent = "请求失败：" + err.message;
            hint.style.color = "#ef4444";
        }
    }

    async function stopPipeline() {
        if (!confirm("确定停止当前检测任务？已完成的检测结果会保留。")) return;
        const hint = $("action-hint");
        hint.textContent = "";
        try {
            const resp = await fetch("/api/stop", { method: "POST" });
            const data = await resp.json().catch(() => ({}));
            if (resp.ok && data.ok) {
                hint.textContent = data.message || "已请求停止";
                hint.style.color = "#f5a623";
            } else {
                hint.textContent = data.message || data.detail || `停止失败 (${resp.status})`;
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
        $("btn-stop").addEventListener("click", stopPipeline);

        $("btn-refresh-nodes").addEventListener("click", () => {
            currentPage = 1;
            loadNodes();
        });
        $("btn-add-node").addEventListener("click", openCreateModal);

        let searchTimer = null;
        $("node-search").addEventListener("input", () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                currentPage = 1;
                selectedIds.clear();
                loadNodes();
            }, 250);
        });
        $("node-filter").addEventListener("change", () => {
            currentPage = 1;
            selectedIds.clear();
            loadNodes();
        });

        $("page-first").addEventListener("click", () => gotoPage(1));
        $("page-prev").addEventListener("click", () => gotoPage(currentPage - 1));
        $("page-next").addEventListener("click", () => gotoPage(currentPage + 1));
        $("page-last").addEventListener("click", () => gotoPage(totalPages));

        $("page-size").addEventListener("change", (e) => {
            pageSize = parseInt(e.target.value, 10) || DEFAULT_PAGE_SIZE;
            savePageSize(pageSize);
            currentPage = 1;
            selectedIds.clear();
            loadNodes();
        });

        document.addEventListener("change", (e) => {
            if (e.target && e.target.id === "node-check-all") {
                const checked = e.target.checked;
                if (checked) {
                    currentPageIds.forEach((id) => selectedIds.add(id));
                } else {
                    currentPageIds.forEach((id) => selectedIds.delete(id));
                }
                document.querySelectorAll(".row-check").forEach((cb) => {
                    cb.checked = checked;
                });
                updateBatchButtons();
            }
        });

        $("btn-batch-enable").addEventListener("click", () => batchAction("enable"));
        $("btn-batch-disable").addEventListener("click", () => batchAction("disable"));
        $("btn-batch-retest").addEventListener("click", () => batchAction("retest"));
        $("btn-batch-delete").addEventListener("click", () => batchAction("delete"));

        $("btn-close-edit").addEventListener("click", closeEditModal);
        $("btn-cancel-edit").addEventListener("click", closeEditModal);
        $("btn-delete-node").addEventListener("click", deleteFromModal);
        $("edit-form").addEventListener("submit", submitEdit);
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
    function initPageSizeSelect() {
        const select = $("page-size");
        select.value = String(pageSize);
    }

    setupApiLinks();
    initPageSizeSelect();
    initLogView();
    bind();
    buildTableHeader();
    connect();
    loadNodes();
})();