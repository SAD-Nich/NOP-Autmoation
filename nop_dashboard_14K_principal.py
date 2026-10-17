import json
import sqlite3
import subprocess
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_DIR = Path(__file__).resolve().parent
LATEST_JSON = PROJECT_DIR / "nop_latest.json"
HISTORY_DB = PROJECT_DIR / "nop_dashboard_history.db"
SETTINGS_FILE = PROJECT_DIR / "nop_dashboard_settings.json"

NAME_OPTIONS = ["Andi", "Ricardo", "Budi", "Othniel", "Rionald", "Arif", "Niko", "Daffa", "Wahyu"]
DEFAULT_SETTINGS = {
    "selected_name": "Niko",
    "principals": {
        "maxco": [
            {"symbol": "CHFJPY", "value": -10.0},
            {"symbol": "XAUUSD", "value": 2.0},
            {"symbol": "USDJPY", "value": -15.0},
        ],
        "monex": [
            {"symbol": "XAG", "value": 1.0},
        ],
    },
}

HOST = "127.0.0.1"
PORT = 8765

REFRESH_LOCK = threading.Lock()


def find_collector_exe():
    candidates = [
        PROJECT_DIR / "x64" / "Debug" / "AutomationTrx.exe",
        PROJECT_DIR / "x64" / "Release" / "AutomationTrx.exe",
        PROJECT_DIR / "AutomationTrx.exe",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        "AutomationTrx.exe tidak ditemukan. Build project dulu. "
        "Dashboard mencari x64\\Debug\\AutomationTrx.exe, "
        "x64\\Release\\AutomationTrx.exe, atau AutomationTrx.exe."
    )


def normalize_principal_rows(rows):
    normalized = []
    for row in rows or []:
        symbol = str(row.get("symbol", "")).strip()
        if not symbol:
            continue
        try:
            value = float(row.get("value", 0))
        except (TypeError, ValueError):
            value = 0.0
        normalized.append({"symbol": symbol, "value": value})
    return normalized


def normalize_settings(data):
    data = data or {}
    selected_name = str(data.get("selected_name", DEFAULT_SETTINGS["selected_name"])).strip()
    if selected_name not in NAME_OPTIONS:
        selected_name = DEFAULT_SETTINGS["selected_name"]

    principals = data.get("principals", {}) or {}
    return {
        "selected_name": selected_name,
        "principals": {
            "maxco": normalize_principal_rows(principals.get("maxco", DEFAULT_SETTINGS["principals"]["maxco"])),
            "monex": normalize_principal_rows(principals.get("monex", DEFAULT_SETTINGS["principals"]["monex"])),
        },
        "name_options": list(NAME_OPTIONS),
    }


def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return normalize_settings(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    settings = normalize_settings(DEFAULT_SETTINGS)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    return settings


def save_settings(data):
    settings = normalize_settings(data)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    return settings


def db_connect():
    con = sqlite3.connect(HISTORY_DB)
    con.row_factory = sqlite3.Row
    return con


def init_history_db():
    with db_connect() as con:
        con.execute(
            '''
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                nop_time TEXT NOT NULL,
                data_json TEXT NOT NULL
            )
            '''
        )
        con.commit()


def normalize_snapshot(data):
    return {
        "nop_time": str(data.get("nop_time", "")),
        "spa": [
            {"symbol": str(x["symbol"]), "total": float(x["total"])}
            for x in data.get("spa", [])
        ],
        "stocks": [
            {"symbol": str(x["symbol"]), "total": float(x["total"])}
            for x in data.get("stocks", [])
        ],
        "spa14": [
            {"symbol": str(x["symbol"]), "total": float(x["total"])}
            for x in data.get("spa14", [])
        ],
        "unmapped_14k_suffixes": [
            int(x) for x in data.get("unmapped_14k_suffixes", [])
        ],
        "principals": {
            "maxco": normalize_principal_rows((data.get("principals", {}) or {}).get("maxco", [])),
            "monex": normalize_principal_rows((data.get("principals", {}) or {}).get("monex", [])),
        },
        "selected_name": str(data.get("selected_name", "")).strip(),
    }


def store_snapshot(data):
    data = normalize_snapshot(data)
    captured_at = datetime.now().isoformat(timespec="seconds")

    with db_connect() as con:
        cur = con.execute(
            "INSERT INTO snapshots(captured_at, nop_time, data_json) VALUES (?, ?, ?)",
            (captured_at, data["nop_time"], json.dumps(data, separators=(",", ":"))),
        )
        snapshot_id = cur.lastrowid

        con.execute(
            '''
            DELETE FROM snapshots
            WHERE id NOT IN (
                SELECT id FROM snapshots ORDER BY id DESC LIMIT 250
            )
            '''
        )
        con.commit()

    return snapshot_id


def row_to_snapshot(row):
    if row is None:
        return None
    data = normalize_snapshot(json.loads(row["data_json"]))
    data["id"] = row["id"]
    data["captured_at"] = row["captured_at"]
    return data


def get_snapshot(snapshot_id):
    with db_connect() as con:
        row = con.execute(
            "SELECT id, captured_at, nop_time, data_json FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
    return row_to_snapshot(row)


def get_latest_snapshot():
    with db_connect() as con:
        row = con.execute(
            '''
            SELECT id, captured_at, nop_time, data_json
            FROM snapshots
            ORDER BY id DESC
            LIMIT 1
            '''
        ).fetchone()
    return row_to_snapshot(row)


def get_previous_snapshot(current_id):
    with db_connect() as con:
        row = con.execute(
            '''
            SELECT id, captured_at, nop_time, data_json
            FROM snapshots
            WHERE id < ?
            ORDER BY id DESC
            LIMIT 1
            ''',
            (current_id,),
        ).fetchone()
    return row_to_snapshot(row)


def get_history(limit=40):
    with db_connect() as con:
        rows = con.execute(
            '''
            SELECT id, captured_at, nop_time
            FROM snapshots
            ORDER BY id DESC
            LIMIT ?
            ''',
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def run_collector():
    exe = find_collector_exe()
    cmd = [str(exe), "--nop-only", "--no-pause"]

    with REFRESH_LOCK:
        proc = subprocess.run(
            cmd,
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        if proc.returncode != 0:
            details = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(
                f"AutomationTrx.exe gagal (exit code {proc.returncode}).\n{details}"
            )

        if not LATEST_JSON.exists():
            raise RuntimeError("Collector selesai tetapi nop_latest.json tidak ditemukan.")

        try:
            data = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"nop_latest.json tidak bisa dibaca: {exc}") from exc

        settings = load_settings()
        data["principals"] = settings["principals"]
        data["selected_name"] = settings["selected_name"]
        snapshot_id = store_snapshot(data)
        return get_snapshot(snapshot_id)


def values_map(snapshot):
    result = {}
    if not snapshot:
        return result
    for section in ("spa", "stocks", "spa14"):
        for row in snapshot.get(section, []):
            result[(section, row["symbol"])] = float(row["total"])

    principals = snapshot.get("principals", {}) or {}
    for row in principals.get("maxco", []):
        result[("principal_maxco", row["symbol"])] = float(row["value"])
    for row in principals.get("monex", []):
        result[("principal_monex", row["symbol"])] = float(row["value"])
    return result


def make_comparison(current, baseline):
    if not current:
        return []

    current_map = values_map(current)
    baseline_map = values_map(baseline)
    keys = set(current_map) | set(baseline_map)
    changes = []

    for section, symbol in keys:
        old = baseline_map.get((section, symbol), 0.0)
        new = current_map.get((section, symbol), 0.0)
        delta = new - old

        if abs(delta) < 1e-9:
            delta = 0.0

        changes.append(
            {
                "section": section,
                "symbol": symbol,
                "old": old,
                "new": new,
                "delta": delta,
                "is_new": (section, symbol) not in baseline_map,
                "is_removed": (section, symbol) not in current_map,
            }
        )

    changes.sort(key=lambda x: (-abs(x["delta"]), x["symbol"]))
    return changes


def state_payload(baseline_id=None):
    current = get_latest_snapshot()
    baseline = None

    if current:
        if baseline_id is not None:
            baseline = get_snapshot(baseline_id)
        if baseline is None:
            baseline = get_previous_snapshot(current["id"])

    settings = load_settings()
    if current:
        current["principals"] = settings["principals"]
        current["selected_name"] = settings["selected_name"]

    return {
        "current": current,
        "baseline": baseline,
        "changes": make_comparison(current, baseline),
        "history": get_history(),
        "settings": settings,
    }


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NOP Dashboard</title>
<style>
:root {
    --bg: #f3f5f7;
    --panel: #ffffff;
    --text: #17202a;
    --muted: #6d7983;
    --border: #dfe5ea;
    --up-bg: #eaf7ee;
    --up-border: #b6dfc1;
    --down-bg: #fff0f0;
    --down-border: #efc0c0;
    --new-bg: #eef3ff;
    --new-border: #c7d5ff;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: "Segoe UI", Arial, sans-serif;
}
.page {
    max-width: 1320px;
    margin: 28px auto;
    padding: 0 20px 50px;
}
.topbar {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 22px 24px;
    box-shadow: 0 8px 28px rgba(0,0,0,.04);
    margin-bottom: 18px;
}
.toprow {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 18px;
    flex-wrap: wrap;
}
h1 { margin: 0; font-size: 25px; }
.time {
    font-weight: 750;
    background: #eef2f5;
    border-radius: 10px;
    padding: 9px 12px;
}
.controls {
    margin-top: 17px;
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
}
button, select {
    font: inherit;
    border: 1px solid #cbd4db;
    background: #fff;
    color: var(--text);
    border-radius: 9px;
    min-height: 40px;
}
button {
    padding: 8px 14px;
    cursor: pointer;
    font-weight: 700;
}
button.primary {
    background: #17202a;
    color: #fff;
    border-color: #17202a;
}
button:disabled { opacity: .55; cursor: wait; }
select { padding: 8px 10px; }
.status {
    color: var(--muted);
    font-size: 13px;
    margin-left: auto;
}
.grid {
    display: grid;
    grid-template-columns: minmax(0, 1.45fr) minmax(320px, .75fr);
    gap: 18px;
}
.panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 16px;
    box-shadow: 0 8px 28px rgba(0,0,0,.035);
    overflow: hidden;
}
.panel-title {
    padding: 18px 20px 5px;
    font-weight: 800;
    font-size: 17px;
}
.panel-subtitle {
    padding: 0 20px 16px;
    color: var(--muted);
    font-size: 13px;
}
.report {
    padding: 18px 20px 22px;
    border-top: 1px solid #edf0f2;
    font-family: Consolas, "Courier New", monospace;
    font-size: 14px;
    line-height: 1.65;
    overflow-x: auto;
}
.report-line {
    min-width: max-content;
    white-space: pre;
    padding: 1px 8px;
    margin: 0 -8px;
    border-left: 3px solid transparent;
    border-radius: 5px;
}
.report-line.heading { font-weight: 800; }
.report-line.separator { color: #727d85; }
.report-line.changed-up {
    background: var(--up-bg);
    border-left-color: var(--up-border);
}
.report-line.changed-down {
    background: var(--down-bg);
    border-left-color: var(--down-border);
}
.report-line.changed-new {
    background: var(--new-bg);
    border-left-color: var(--new-border);
}
.delta {
    display: inline-block;
    margin-left: 14px;
    padding: 0 7px;
    border-radius: 999px;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 11px;
    font-weight: 800;
    vertical-align: 1px;
}
.delta.up { background: #d8f1df; }
.delta.down { background: #ffe0e0; }
.delta.new { background: #dfe7ff; }
.change-list {
    border-top: 1px solid #edf0f2;
    padding: 8px 14px 16px;
}
.change-item {
    border-bottom: 1px solid #edf0f2;
    padding: 12px 7px;
}
.change-item:last-child { border-bottom: 0; }
.change-symbol {
    font-weight: 800;
    margin-bottom: 5px;
}
.change-values {
    font-size: 13px;
    color: #53606a;
    font-variant-numeric: tabular-nums;
}
.change-delta {
    margin-top: 5px;
    font-size: 13px;
    font-weight: 800;
}
.positive { color: #19733a; }
.negative { color: #ae3030; }
.empty { padding: 20px; color: var(--muted); }
.note {
    margin-top: 14px;
    color: var(--muted);
    font-size: 12px;
}
.input-panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 16px;
    box-shadow: 0 8px 28px rgba(0,0,0,.035);
    margin-bottom: 18px;
    overflow: hidden;
}
.input-grid {
    display: grid;
    grid-template-columns: 220px 1fr 1fr;
    gap: 18px;
    padding: 18px 20px 20px;
    border-top: 1px solid #edf0f2;
}
.input-group label {
    display: block;
    font-size: 12px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: .05em;
    color: #65727c;
    margin-bottom: 8px;
}
.name-select { width: 100%; padding: 8px 10px; }
.principal-table { width: 100%; border-collapse: collapse; min-width: 0; }
.principal-table td { border: 0; padding: 4px 4px 4px 0; }
.principal-table input {
    width: 100%; min-height: 36px; border: 1px solid #cbd4db;
    border-radius: 8px; padding: 7px 9px; font: inherit;
    font-variant-numeric: tabular-nums;
}
.principal-table input.value-input { text-align: right; }
.principal-table button { min-height: 36px; padding: 6px 9px; }
.small-button { margin-top: 7px; padding: 7px 10px; font-size: 12px; }
.save-row {
    padding: 0 20px 20px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
}
@media (max-width: 1000px) {
    .input-grid { grid-template-columns: 1fr; }
}
@media (max-width: 900px) {
    .grid { grid-template-columns: 1fr; }
    .status { width: 100%; margin-left: 0; }
}
</style>
</head>
<body>
<div class="page">
    <div class="topbar">
        <div class="toprow">
            <h1>NOP Dashboard</h1>
            <div class="time" id="nopTime">No snapshot yet</div>
        </div>
        <div class="controls">
            <button class="primary" id="refreshBtn" onclick="refreshNOP()">Refresh / Recalculate NOP</button>
            <button id="copyBtn" onclick="copyWhatsApp()" disabled>Copy for WhatsApp</button>

            <label for="baselineSelect"><b>Compare against:</b></label>
            <select id="baselineSelect" onchange="changeBaseline()" disabled>
                <option value="">Previous snapshot</option>
            </select>

            <span class="status" id="status">Click Refresh to pull current MT4 open positions.</span>
        </div>
    </div>

    <section class="input-panel">
        <div class="panel-title">Manual Report Inputs</div>
        <div class="panel-subtitle">Same idea as the Google Sheet: choose the sender name and type Principal values manually.</div>
        <div class="input-grid">
            <div class="input-group">
                <label for="nameSelect">Name</label>
                <select id="nameSelect" class="name-select"></select>
            </div>
            <div class="input-group">
                <label>Principal Maxco</label>
                <table class="principal-table"><tbody id="maxcoRows"></tbody></table>
                <button class="small-button" onclick="addPrincipalRow('maxco')">+ Add Maxco row</button>
            </div>
            <div class="input-group">
                <label>Principal Monex</label>
                <table class="principal-table"><tbody id="monexRows"></tbody></table>
                <button class="small-button" onclick="addPrincipalRow('monex')">+ Add Monex row</button>
            </div>
        </div>
        <div class="save-row">
            <button class="primary" onclick="saveManualInputs()">Save Name & Principal</button>
            <span class="status" id="inputStatus">Saved locally; MT4 refresh will not overwrite these values.</span>
        </div>
    </section>

    <div class="grid">
        <section class="panel">
            <div class="panel-title">WhatsApp-ready Content NOP</div>
            <div class="panel-subtitle">
                Highlighting is visual only. Copy for WhatsApp copies the clean text without delta labels.
            </div>
            <div class="report" id="report">
                <div class="empty">No NOP snapshot yet.</div>
            </div>
        </section>

        <aside class="panel">
            <div class="panel-title">Changes</div>
            <div class="panel-subtitle" id="compareTitle">No comparison available yet.</div>
            <div class="change-list" id="changes">
                <div class="empty">Refresh at least twice to compare two NOP snapshots.</div>
            </div>
        </aside>
    </div>

    <div class="note">
        Delta means change in net lots, not profit/loss. Positive = NOP moved upward; negative = NOP moved downward.
    </div>
</div>

<script>
let state = null;
let cleanReportText = "";
let manualSettings = null;

function fmt(value) {
    let n = Number(value || 0);
    if (Math.abs(n) < 1e-9) n = 0;
    let s = n.toFixed(2);
    s = s.replace(/\.?0+$/, "");
    return s || "0";
}

function signedFmt(value) {
    const n = Number(value || 0);
    if (Math.abs(n) < 1e-9) return "0";
    return (n > 0 ? "+" : "") + fmt(n);
}

function key(section, symbol) {
    return section + "::" + symbol;
}


function principalRows(group) {
    if (!manualSettings || !manualSettings.principals) return [];
    return manualSettings.principals[group] || [];
}

function renderPrincipalEditor(group, elementId) {
    const tbody = document.getElementById(elementId);
    tbody.innerHTML = "";
    principalRows(group).forEach((row, index) => {
        const tr = document.createElement("tr");
        tr.innerHTML =
            '<td><input class="symbol-input" data-group="' + group + '" value="' + escapeHtml(row.symbol || "") + '" placeholder="Symbol"></td>' +
            '<td style="width:110px"><input class="value-input" type="number" step="0.01" data-group="' + group + '" value="' + String(row.value ?? 0) + '" placeholder="0"></td>' +
            '<td style="width:48px"><button onclick="removePrincipalRow(\'' + group + '\',' + index + ')">×</button></td>';
        tbody.appendChild(tr);
    });
}

function renderManualInputs() {
    if (!manualSettings) return;
    const select = document.getElementById("nameSelect");
    select.innerHTML = "";
    for (const name of (manualSettings.name_options || [])) {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        option.selected = name === manualSettings.selected_name;
        select.appendChild(option);
    }
    renderPrincipalEditor("maxco", "maxcoRows");
    renderPrincipalEditor("monex", "monexRows");
}

function captureManualInputs() {
    if (!manualSettings) return;
    manualSettings.selected_name = document.getElementById("nameSelect").value;
    for (const group of ["maxco", "monex"]) {
        const symbols = [...document.querySelectorAll('.symbol-input[data-group="' + group + '"]')];
        const values = [...document.querySelectorAll('.value-input[data-group="' + group + '"]')];
        const rows = [];
        for (let i = 0; i < symbols.length; i++) {
            const symbol = symbols[i].value.trim();
            if (!symbol) continue;
            rows.push({symbol, value: Number(values[i].value || 0)});
        }
        manualSettings.principals[group] = rows;
    }
}

function addPrincipalRow(group) {
    captureManualInputs();
    manualSettings.principals[group].push({symbol: "", value: 0});
    renderManualInputs();
}

function removePrincipalRow(group, index) {
    captureManualInputs();
    manualSettings.principals[group].splice(index, 1);
    renderManualInputs();
}

async function saveManualInputs() {
    const inputStatus = document.getElementById("inputStatus");
    try {
        captureManualInputs();
        inputStatus.textContent = "Saving...";
        const res = await fetch("/api/settings", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(manualSettings),
        });
        const body = await res.json();
        if (!res.ok) throw new Error(body.error || "Could not save inputs.");
        manualSettings = body.settings;
        state = body.state;
        renderManualInputs();
        render();
        inputStatus.textContent = "Saved.";
    } catch (err) {
        inputStatus.textContent = "ERROR: " + err.message;
    }
}

function currentChangeMap() {
    const map = {};
    if (!state) return map;
    for (const c of (state.changes || [])) {
        map[key(c.section, c.symbol)] = c;
    }
    return map;
}

function addLine(container, text, cssClass="", deltaInfo=null) {
    const line = document.createElement("div");
    line.className = "report-line " + cssClass;

    const textSpan = document.createElement("span");
    textSpan.textContent = text;
    line.appendChild(textSpan);

    if (deltaInfo && Math.abs(deltaInfo.delta) > 1e-9) {
        const badge = document.createElement("span");
        const cls = deltaInfo.is_new ? "new" : (deltaInfo.delta > 0 ? "up" : "down");
        badge.className = "delta " + cls;
        badge.textContent = "Δ " + signedFmt(deltaInfo.delta) + " lots";
        line.appendChild(badge);
    }

    container.appendChild(line);
}

function buildCleanReport(snapshot) {
    if (!snapshot) return "";

    const lines = [];
    lines.push("Content NOP");
    lines.push("NOP TIME " + snapshot.nop_time);
    lines.push("Client SPA          Total NOP ( Reguler / Mini / Lumen ) :");

    for (const row of snapshot.spa) {
        lines.push(
            row.symbol + " = " + fmt(row.total) +
            " ( " + fmt(row.total) + " / 0 / 0 )"
        );
    }

    lines.push("-----------------------");
    lines.push("Client US Single Stock");
    lines.push("Total NOP ( Reguler / Mini ) :");

    for (const row of snapshot.stocks) {
        lines.push(
            row.symbol + " = " + fmt(row.total) +
            " ( " + fmt(row.total) + " / 0 )"
        );
    }

    lines.push("-----------------------");
    lines.push("Principal Maxco");
    for (const row of ((snapshot.principals || {}).maxco || [])) {
        lines.push(row.symbol + " " + fmt(row.value));
    }

    lines.push("-----------------------");
    lines.push("Principal Monex");
    for (const row of ((snapshot.principals || {}).monex || [])) {
        lines.push(row.symbol + " " + fmt(row.value));
    }

    lines.push("-----------------------");
    lines.push("NOP KONVERSI 14 RIBU");
    lines.push("Client SPA");

    for (const row of (snapshot.spa14 || [])) {
        lines.push(row.symbol + " = " + fmt(row.total));
    }

    lines.push("-----------------------");
    if (snapshot.selected_name) lines.push(snapshot.selected_name);
    return lines.join("\n");
}

function renderReport() {
    const report = document.getElementById("report");
    report.innerHTML = "";

    if (!state || !state.current) {
        report.innerHTML = '<div class="empty">No NOP snapshot yet.</div>';
        cleanReportText = "";
        document.getElementById("copyBtn").disabled = true;
        return;
    }

    const current = state.current;
    const changes = currentChangeMap();

    addLine(report, "Content NOP", "heading");
    addLine(report, "NOP TIME " + current.nop_time, "heading");
    addLine(report, "Client SPA          Total NOP ( Reguler / Mini / Lumen ) :", "heading");

    for (const row of current.spa) {
        const c = changes[key("spa", row.symbol)];
        let css = "";
        if (c && Math.abs(c.delta) > 1e-9) {
            css = c.is_new ? "changed-new" : (c.delta > 0 ? "changed-up" : "changed-down");
        }

        addLine(
            report,
            row.symbol + " = " + fmt(row.total) +
                " ( " + fmt(row.total) + " / 0 / 0 )",
            css,
            c
        );
    }

    addLine(report, "-----------------------", "separator");
    addLine(report, "Client US Single Stock", "heading");
    addLine(report, "Total NOP ( Reguler / Mini ) :", "heading");

    for (const row of current.stocks) {
        const c = changes[key("stocks", row.symbol)];
        let css = "";
        if (c && Math.abs(c.delta) > 1e-9) {
            css = c.is_new ? "changed-new" : (c.delta > 0 ? "changed-up" : "changed-down");
        }

        addLine(
            report,
            row.symbol + " = " + fmt(row.total) +
                " ( " + fmt(row.total) + " / 0 )",
            css,
            c
        );
    }

    addLine(report, "-----------------------", "separator");
    addLine(report, "Principal Maxco", "heading");

    for (const row of (((current.principals || {}).maxco) || [])) {
        const c = changes[key("principal_maxco", row.symbol)];
        let css = "";
        if (c && Math.abs(c.delta) > 1e-9) {
            css = c.is_new ? "changed-new" : (c.delta > 0 ? "changed-up" : "changed-down");
        }
        addLine(report, row.symbol + " " + fmt(row.value), css, c);
    }

    addLine(report, "-----------------------", "separator");
    addLine(report, "Principal Monex", "heading");

    for (const row of (((current.principals || {}).monex) || [])) {
        const c = changes[key("principal_monex", row.symbol)];
        let css = "";
        if (c && Math.abs(c.delta) > 1e-9) {
            css = c.is_new ? "changed-new" : (c.delta > 0 ? "changed-up" : "changed-down");
        }
        addLine(report, row.symbol + " " + fmt(row.value), css, c);
    }

    addLine(report, "-----------------------", "separator");
    addLine(report, "NOP KONVERSI 14 RIBU", "heading");
    addLine(report, "Client SPA", "heading");

    for (const row of (current.spa14 || [])) {
        const c = changes[key("spa14", row.symbol)];
        let css = "";

        if (c && Math.abs(c.delta) > 1e-9) {
            css = c.is_new ? "changed-new" : (c.delta > 0 ? "changed-up" : "changed-down");
        }

        addLine(
            report,
            row.symbol + " = " + fmt(row.total),
            css,
            c
        );
    }

    addLine(report, "-----------------------", "separator");

    if ((current.unmapped_14k_suffixes || []).length) {
        addLine(
            report,
            "[WARNING] Unmapped 14K login suffix: " +
                current.unmapped_14k_suffixes.join(", "),
            "changed-down"
        );
    }

    if (current.selected_name) {
        addLine(report, current.selected_name, "heading");
    }

    cleanReportText = buildCleanReport(current);
    document.getElementById("copyBtn").disabled = false;
}

function renderChanges() {
    const list = document.getElementById("changes");
    const title = document.getElementById("compareTitle");
    list.innerHTML = "";

    if (!state || !state.current || !state.baseline) {
        title.textContent = "No comparison available yet.";
        list.innerHTML = '<div class="empty">Refresh at least twice to compare two NOP snapshots.</div>';
        return;
    }

    title.textContent = state.baseline.nop_time + "  →  " + state.current.nop_time;

    const changed = (state.changes || []).filter(c => Math.abs(c.delta) > 1e-9);

    if (!changed.length) {
        list.innerHTML = '<div class="empty">No NOP changes versus the selected snapshot.</div>';
        return;
    }

    for (const c of changed) {
        const item = document.createElement("div");
        item.className = "change-item";

        let label = "";
        if (c.is_new) label = "New exposure";
        else if (c.is_removed) label = "No longer in current snapshot";

        const directionClass = c.delta > 0 ? "positive" : "negative";
        const sectionLabel =
            c.section === "spa14" ? "14K Conversion" :
            c.section === "stocks" ? "US Single Stock" :
            c.section === "principal_maxco" ? "Principal Maxco" :
            c.section === "principal_monex" ? "Principal Monex" :
            "Client SPA";

        item.innerHTML =
            '<div class="change-symbol">' +
                escapeHtml(c.symbol) +
                ' · ' + escapeHtml(sectionLabel) +
            '</div>' +
            '<div class="change-values">' +
                fmt(c.old) + ' → ' + fmt(c.new) +
                (label ? ' · ' + escapeHtml(label) : '') +
            '</div>' +
            '<div class="change-delta ' + directionClass + '">' +
                'Δ ' + signedFmt(c.delta) + ' lots' +
            '</div>';

        list.appendChild(item);
    }
}

function renderHistory() {
    const select = document.getElementById("baselineSelect");
    const previousValue = select.value;

    select.innerHTML = '<option value="">Previous snapshot</option>';

    if (!state || !state.current) {
        select.disabled = true;
        return;
    }

    for (const h of (state.history || [])) {
        if (h.id === state.current.id) continue;

        const opt = document.createElement("option");
        opt.value = String(h.id);
        opt.textContent = h.nop_time + "  [#" + h.id + "]";
        select.appendChild(opt);
    }

    if ([...select.options].some(o => o.value === previousValue)) {
        select.value = previousValue;
    }

    select.disabled = select.options.length <= 1;
}

function render() {
    const timeBox = document.getElementById("nopTime");
    timeBox.textContent =
        (state && state.current)
        ? "NOP TIME " + state.current.nop_time
        : "No snapshot yet";

    renderHistory();
    renderReport();
    renderChanges();
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

async function loadState(baselineId="") {
    const query = baselineId ? "?baseline_id=" + encodeURIComponent(baselineId) : "";
    const res = await fetch("/api/state" + query);
    if (!res.ok) throw new Error(await res.text());
    state = await res.json();
    manualSettings = JSON.parse(JSON.stringify(state.settings || {}));
    renderManualInputs();
    render();
}

async function refreshNOP() {
    const btn = document.getElementById("refreshBtn");
    const status = document.getElementById("status");

    btn.disabled = true;
    status.textContent = "Reading current MT4 positions and recalculating NOP...";

    try {
        const res = await fetch("/api/refresh", { method: "POST" });
        const body = await res.json();

        if (!res.ok) {
            throw new Error(body.error || "Refresh failed.");
        }

        state = body;
        manualSettings = JSON.parse(JSON.stringify(state.settings || manualSettings || {}));
        document.getElementById("baselineSelect").value = "";
        renderManualInputs();
        render();
        status.textContent = "Refresh complete.";
    } catch (err) {
        status.textContent = "ERROR: " + err.message;
        alert(err.message);
    } finally {
        btn.disabled = false;
    }
}

async function changeBaseline() {
    const value = document.getElementById("baselineSelect").value;
    const status = document.getElementById("status");

    try {
        status.textContent = "Changing comparison snapshot...";
        await loadState(value);
        status.textContent = "Comparison updated.";
    } catch (err) {
        status.textContent = "ERROR: " + err.message;
    }
}

async function copyWhatsApp() {
    if (!cleanReportText) return;

    try {
        await navigator.clipboard.writeText(cleanReportText);
        document.getElementById("status").textContent = "WhatsApp text copied.";
    } catch (err) {
        const area = document.createElement("textarea");
        area.value = cleanReportText;
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
        document.getElementById("status").textContent = "WhatsApp text copied.";
    }
}

loadState().catch(err => {
    document.getElementById("status").textContent = "ERROR: " + err.message;
});
</script>
</body>
</html>
'''


class Handler(BaseHTTPRequestHandler):
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/state":
            try:
                qs = parse_qs(parsed.query)
                raw = qs.get("baseline_id", [""])[0].strip()
                baseline_id = int(raw) if raw else None
                self.send_json(state_payload(baseline_id))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/refresh":
            try:
                run_collector()
                self.send_json(state_payload())
            except subprocess.TimeoutExpired:
                self.send_json(
                    {"error": "AutomationTrx.exe melewati timeout 5 menit."},
                    500,
                )
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return

        if parsed.path == "/api/settings":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(content_length) if content_length else b"{}"
                payload = json.loads(raw.decode("utf-8"))
                settings = save_settings(payload)
                self.send_json({"settings": settings, "state": state_payload()})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return

        self.send_error(404)

    def log_message(self, fmt, *args):
        return


def main():
    init_history_db()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"

    print("=" * 64)
    print("NOP Dashboard")
    print(f"Open: {url}")
    print("Refresh button runs AutomationTrx.exe in NOP-only read mode.")
    print("Press Ctrl+C in this window to stop the dashboard.")
    print("=" * 64)

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
