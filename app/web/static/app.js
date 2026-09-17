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

    // ---------------------------------------------------- 配置 API 链接
    function setupApiLinks() {
        const base = `${location.protocol}//${location.hostname}:8110`;
        document.querySelectorAll("a.btn-link").forEach((a) => {
            const fmt = a.dataset.fmt;
            if (fmt) a.href = `${base}/sub/${fmt}`;
        });
    }

    // ---------------------------------------------------- 渲染
    function render(s) {
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
        $("last-updated").textContent = s.last_updated || "-";

        const btn = $("btn-trigger");
        btn.disabled = !!s.running;
        btn.textContent = s.running ? "运行中..." : "立即执行一次";
    }

    // ---------------------------------------------------- WebSocket
    let ws = null;
    let reconnectTimer = null;

    function connect() {
        const proto = location.protocol === "https:" ? "wss" : "ws";
        ws = new WebSocket(`${proto}://${location.host}/ws/status`);

        ws.onopen = () => {
            $("ws-state").textContent = "WebSocket 已连接";
        };

        ws.onmessage = (e) => {
            try {
                render(JSON.parse(e.data));
            } catch (err) {
                console.error("状态解析失败", err);
            }
        };

        ws.onclose = () => {
            $("ws-state").textContent = "WebSocket 已断开，2 秒后重连...";
            clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connect, 2000);
        };

        ws.onerror = () => {
            try { ws.close(); } catch (_) { }
        };
    }

    // ---------------------------------------------------- 手动触发
    function bindTrigger() {
        $("btn-trigger").addEventListener("click", async () => {
            const hint = $("action-hint");
            hint.textContent = "";
            hint.style.color = "";

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
        });
    }

    // ---------------------------------------------------- 启动
    setupApiLinks();
    bindTrigger();
    connect();
})();