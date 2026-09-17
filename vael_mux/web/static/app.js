(() => {
    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => document.querySelectorAll(sel);

    // ---- tabs ----
    $$(".tab").forEach((btn) => {
        btn.addEventListener("click", () => {
            $$(".tab").forEach((b) => b.classList.remove("active"));
            $$(".tab-panel").forEach((p) => p.classList.remove("active"));
            btn.classList.add("active");
            $("#tab-" + btn.dataset.tab).classList.add("active");
            if (btn.dataset.tab === "config") loadConfig();
            if (btn.dataset.tab === "nodes") loadNodes();
        });
    });

    // ---- kernel status ----
    async function refreshKernelStatus() {
        try {
            const r = await fetch("/api/kernel");
            const d = await r.json();
            const el = $("#kernel-status");
            el.textContent = d.running
                ? `内核: 运行中 ${d.version ? "(" + d.version + ")" : ""}`
                : "内核: 未运行";
            el.style.color = d.running ? "var(--ok)" : "var(--err)";
        } catch { }
    }
    setInterval(refreshKernelStatus, 8000);
    refreshKernelStatus();

    // ---- nodes ----
    let nodeCache = [];
    async function loadNodes() {
        const r = await fetch("/api/nodes");
        const d = await r.json();
        nodeCache = d.nodes;
        $("#stat-nodes").textContent = d.total;
        $("#stat-alive").textContent = d.nodes.filter((n) => n.status && n.status.alive).length;
        $("#stat-mode").textContent = d.sort_mode;
        renderNodes();
    }

    function renderNodes() {
        const filter = ($("#node-filter")?.value || "").toLowerCase();
        const tbody = $("#nodes-table tbody");
        tbody.innerHTML = "";
        nodeCache.forEach((n, i) => {
            if (filter && !(n.name.toLowerCase().includes(filter) || n.server.toLowerCase().includes(filter)))
                return;
            const s = n.status || {};
            const tr = document.createElement("tr");
            tr.innerHTML = `
        <td>${i + 1}</td>
        <td>${escapeHtml(n.name)}</td>
        <td>${n.protocol}</td>
        <td>${escapeHtml(n.server)}</td>
        <td>${n.port}</td>
        <td class="${s.alive ? "alive-ok" : "alive-no"}">${s.alive ? "✓" : "✗"}</td>
        <td>${s.latency_ms ? s.latency_ms.toFixed(1) : "-"}</td>
        <td>${s.download_mbps ? s.download_mbps.toFixed(2) : "-"}</td>
        <td>${s.stability ? (s.stability * 100).toFixed(0) + "%" : "-"}</td>
        <td>${n.score ? n.score.toFixed(1) : "-"}</td>
      `;
            tbody.appendChild(tr);
        });
    }

    $("#node-filter")?.addEventListener("input", renderNodes);
    $("#btn-nodes-reload")?.addEventListener("click", loadNodes);

    // ---- actions ----
    async function call(url) {
        const btn = document.querySelector(`[data-call="${url}"]`);
        try {
            const r = await fetch(url, { method: "POST" });
            if (!r.ok) throw new Error(await r.text());
            await loadNodes();
        } catch (e) {
            alert("操作失败: " + e.message);
        }
    }

    $("#btn-refresh")?.addEventListener("click", async () => {
        $("#btn-refresh").disabled = true;
        try {
            await fetch("/api/refresh", { method: "POST" });
            await loadNodes();
            await refreshKernelStatus();
        } finally {
            $("#btn-refresh").disabled = false;
        }
    });
    $("#btn-alive")?.addEventListener("click", () => call("/api/test/alive"));
    $("#btn-speed")?.addEventListener("click", () => call("/api/test/speed"));

    // ---- config ----
    async function loadConfig() {
        const r = await fetch("/api/config");
        const d = await r.json();
        $("#cfg-editor").value = JSON.stringify(d, null, 2);
        $("#cfg-msg").textContent = "";
    }
    $("#btn-cfg-load")?.addEventListener("click", loadConfig);
    $("#btn-cfg-save")?.addEventListener("click", async () => {
        try {
            const obj = JSON.parse($("#cfg-editor").value);
            const r = await fetch("/api/config", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(obj),
            });
            if (!r.ok) throw new Error(await r.text());
            $("#cfg-msg").textContent = "已保存 ✓";
        } catch (e) {
            $("#cfg-msg").textContent = "错误: " + e.message;
        }
    });

    // ---- logs ----
    const logView = $("#log-view");
    $("#btn-clear-logs")?.addEventListener("click", () => (logView.textContent = ""));
    let ws;
    function connectWS() {
        const proto = location.protocol === "https:" ? "wss" : "ws";
        ws = new WebSocket(`${proto}://${location.host}/ws/logs`);
        ws.onmessage = (e) => {
            try {
                const p = JSON.parse(e.data);
                if (p.type === "log") appendLog(p.data);
            } catch { }
        };
        ws.onclose = () => setTimeout(connectWS, 2000);
    }
    function appendLog(entry) {
        const line = document.createElement("div");
        line.className = "log-" + (entry.level || "INFO");
        line.textContent = `[${entry.ts}] [${entry.level}] ${entry.name}: ${entry.msg}`;
        logView.appendChild(line);
        // 滚动到底部
        logView.scrollTop = logView.scrollHeight;
        // 控制缓冲
        while (logView.childNodes.length > 3000) logView.removeChild(logView.firstChild);
    }
    connectWS();

    // ---- misc ----
    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    // 初始化
    loadNodes();
    setInterval(() => {
        if ($("#tab-nodes").classList.contains("active")) loadNodes();
        if ($("#tab-overview").classList.contains("active")) {
            $("#stat-last").textContent = new Date().toLocaleTimeString();
        }
    }, 5000);
})();