#!/usr/bin/env python3
"""
Cowork Archive Manager
Claude Desktop (Cowork) のアーカイブ済みセッションを管理するGUIツール

起動すると localhost でサーバーが立ち上がり、ブラウザに管理画面が表示されます。
ブラウザタブを閉じると自動的にサーバーも終了します。

GitHub: https://github.com/YOUR_USERNAME/cowork-archive-manager
License: MIT
"""

import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# --- 定数 ---
VERSION = "1.0.0"
APP_TITLE = "Cowork Archive Manager"
PORT = 52849
LOCK_FILE = Path.home() / ".cowork_archive_manager.lock"

def get_sessions_base():
    """OSに応じたセッションディレクトリのベースパスを返す"""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"
    elif system == "Windows":
        return Path(os.environ.get("APPDATA", "")) / "Claude" / "local-agent-mode-sessions"
    elif system == "Linux":
        return Path.home() / ".config" / "Claude" / "local-agent-mode-sessions"
    else:
        return Path.home() / ".config" / "Claude" / "local-agent-mode-sessions"


# --- プロセス管理 ---

def is_server_running():
    """既にサーバーが動いているか確認"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", PORT))
        s.close()
        return True
    except (ConnectionRefusedError, OSError):
        return False


def kill_existing_server():
    """既存のサーバープロセスを終了"""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
        except (ValueError, ProcessLookupError, OSError):
            pass
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


def write_lock():
    """PIDをロックファイルに書き込み"""
    LOCK_FILE.write_text(str(os.getpid()))


def remove_lock():
    """ロックファイルを削除"""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# --- セッション操作 ---

def find_sessions_dir():
    """セッションディレクトリを探索"""
    sessions_base = get_sessions_base()
    if not sessions_base.exists():
        return None
    for org_dir in sessions_base.iterdir():
        if not org_dir.is_dir() or org_dir.name == "skills-plugin":
            continue
        for proj_dir in org_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            if any(proj_dir.glob("local_*.json")):
                return proj_dir
    return None


def load_sessions():
    """全セッションを読み込み"""
    sessions_dir = find_sessions_dir()
    if sessions_dir is None:
        return []
    sessions = []
    for json_file in sessions_dir.glob("local_*.json"):
        try:
            with open(json_file) as f:
                data = json.load(f)
            data["_path"] = str(json_file)
            sessions.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    sessions.sort(key=lambda s: s.get("lastActivityAt", 0), reverse=True)
    return sessions


def restore_session(json_path):
    """セッションを復元（isArchived を false に変更）"""
    try:
        with open(json_path) as f:
            data = json.load(f)
        data["isArchived"] = False
        with open(json_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except (json.JSONDecodeError, OSError):
        return False


def delete_session(json_path):
    """セッションを完全削除（JSONファイルと関連ディレクトリ）"""
    try:
        p = Path(json_path)
        p.unlink(missing_ok=True)
        dir_path = p.with_suffix("")
        if dir_path.is_dir():
            shutil.rmtree(dir_path)
        return True
    except OSError:
        return False


# --- HTML テンプレート ---

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cowork Archive Manager</title>
<style>
  :root {
    --bg: #1a1a2e;
    --surface: #16213e;
    --surface2: #0f3460;
    --accent: #e94560;
    --accent2: #533483;
    --text: #eee;
    --text2: #aab;
    --success: #4ecca3;
    --warning: #f0a500;
    --danger: #e94560;
    --border: #2a2a4a;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }
  .header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 16px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .header h1 {
    font-size: 20px;
    font-weight: 600;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .header-actions { display: flex; gap: 8px; }
  .btn {
    padding: 8px 16px;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    transition: all 0.2s;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .btn:hover { transform: translateY(-1px); }
  .btn:active { transform: translateY(0); }
  .btn-primary { background: var(--accent); color: white; }
  .btn-primary:hover { background: #d13550; }
  .btn-secondary { background: var(--surface2); color: var(--text); }
  .btn-secondary:hover { background: #1a4a7a; }
  .btn-success { background: var(--success); color: #1a1a2e; }
  .btn-success:hover { background: #3db893; }
  .btn-warning { background: var(--warning); color: #1a1a2e; }
  .btn-warning:hover { background: #d89400; }
  .btn-danger { background: var(--danger); color: white; }
  .btn-danger:hover { background: #d13550; }
  .btn-ghost { background: transparent; color: var(--text2); border: 1px solid var(--border); }
  .btn-ghost:hover { background: var(--surface2); color: var(--text); }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

  .toolbar {
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }
  .filter-group { display: flex; gap: 4px; }
  .filter-btn {
    padding: 6px 14px;
    border: 1px solid var(--border);
    border-radius: 20px;
    background: transparent;
    color: var(--text2);
    cursor: pointer;
    font-size: 13px;
    transition: all 0.2s;
  }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .filter-btn.active {
    background: var(--accent);
    border-color: var(--accent);
    color: white;
  }
  .count-badge {
    font-size: 13px;
    color: var(--text2);
    padding: 6px 12px;
    background: var(--surface);
    border-radius: 20px;
  }

  .bulk-actions {
    padding: 8px 24px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  .session-list {
    padding: 0 24px 24px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .session-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 20px;
    display: grid;
    grid-template-columns: 32px 1fr auto;
    gap: 16px;
    align-items: center;
    transition: all 0.2s;
    cursor: pointer;
  }
  .session-card:hover {
    border-color: var(--accent2);
    background: #1a2a4e;
  }
  .session-card.selected {
    border-color: var(--accent);
    background: #2a1a3e;
  }
  .session-checkbox {
    width: 20px;
    height: 20px;
    accent-color: var(--accent);
    cursor: pointer;
  }
  .session-info { min-width: 0; }
  .session-name {
    font-size: 15px;
    font-weight: 600;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .session-meta {
    font-size: 12px;
    color: var(--text2);
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 4px;
  }
  .session-message {
    font-size: 13px;
    color: var(--text2);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 600px;
  }
  .badge {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 500;
  }
  .badge-archived { background: rgba(240,165,0,0.15); color: var(--warning); }
  .badge-active { background: rgba(78,204,163,0.15); color: var(--success); }
  .session-actions {
    display: flex;
    gap: 6px;
    flex-shrink: 0;
  }

  .toast {
    position: fixed;
    bottom: 24px;
    left: 50%;
    transform: translateX(-50%) translateY(100px);
    padding: 12px 24px;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 500;
    z-index: 1000;
    transition: transform 0.3s ease;
    max-width: 90vw;
  }
  .toast.show { transform: translateX(-50%) translateY(0); }
  .toast-success { background: var(--success); color: #1a1a2e; }
  .toast-error { background: var(--danger); color: white; }

  .modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 200;
  }
  .modal {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 24px;
    max-width: 480px;
    width: 90%;
  }
  .modal h2 { font-size: 18px; margin-bottom: 12px; }
  .modal p { color: var(--text2); margin-bottom: 16px; line-height: 1.6; }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; }

  .empty-state {
    text-align: center;
    padding: 60px 24px;
    color: var(--text2);
  }
  .empty-state p { font-size: 16px; }

  .select-all-area {
    padding: 4px 24px 0;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--text2);
  }
</style>
</head>
<body>

<div class="header">
  <h1>Cowork Archive Manager</h1>
  <div class="header-actions">
    <button class="btn btn-ghost" onclick="openFolder()">フォルダを開く</button>
    <button class="btn btn-secondary" onclick="refresh()">更新</button>
  </div>
</div>

<div class="toolbar">
  <div class="filter-group">
    <button class="filter-btn active" data-filter="all" onclick="setFilter('all')">すべて</button>
    <button class="filter-btn" data-filter="archived" onclick="setFilter('archived')">アーカイブ済み</button>
    <button class="filter-btn" data-filter="active" onclick="setFilter('active')">アクティブ</button>
  </div>
  <span class="count-badge" id="count"></span>
</div>

<div class="bulk-actions">
  <button class="btn btn-success" onclick="restoreSelected()" id="btn-restore-sel" disabled>選択を復元</button>
  <button class="btn btn-danger" onclick="deleteSelected()" id="btn-delete-sel" disabled>選択を削除</button>
  <button class="btn btn-warning" onclick="restoreAllArchived()">全アーカイブを復元</button>
  <button class="btn btn-danger" onclick="deleteAllArchived()">全アーカイブを削除</button>
</div>

<div class="select-all-area">
  <input type="checkbox" id="select-all" class="session-checkbox" onchange="toggleSelectAll(this.checked)">
  <label for="select-all">すべて選択</label>
</div>

<div class="session-list" id="session-list"></div>

<div class="toast" id="toast"></div>

<script>
let sessions = [];
let currentFilter = 'all';
let selectedPaths = new Set();

// ブラウザタブが開いている間、サーバーに生存通知を送る
setInterval(() => {
  fetch('/api/heartbeat', { method: 'POST' }).catch(() => {});
}, 2000);

async function api(endpoint, data) {
  const res = await fetch('/api/' + endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data || {})
  });
  return res.json();
}

function formatDate(ms) {
  if (!ms) return '不明';
  const d = new Date(ms);
  return d.getFullYear() + '-' +
    String(d.getMonth()+1).padStart(2,'0') + '-' +
    String(d.getDate()).padStart(2,'0') + ' ' +
    String(d.getHours()).padStart(2,'0') + ':' +
    String(d.getMinutes()).padStart(2,'0');
}

function setFilter(f) {
  currentFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.filter === f);
  });
  renderSessions();
}

function getFiltered() {
  return sessions.filter(s => {
    if (currentFilter === 'archived') return s.isArchived;
    if (currentFilter === 'active') return !s.isArchived;
    return true;
  });
}

function renderSessions() {
  const list = document.getElementById('session-list');
  const filtered = getFiltered();
  document.getElementById('count').textContent = filtered.length + ' 件';

  if (filtered.length === 0) {
    list.innerHTML = '<div class="empty-state"><p>該当するセッションはありません</p></div>';
    return;
  }

  list.innerHTML = filtered.map(s => {
    const isSelected = selectedPaths.has(s._path);
    const badge = s.isArchived
      ? '<span class="badge badge-archived">アーカイブ</span>'
      : '<span class="badge badge-active">アクティブ</span>';
    const msg = (s.initialMessage || '').replace(/\n/g, ' ').substring(0, 80);
    const model = s.model || '不明';
    return `
      <div class="session-card ${isSelected ? 'selected' : ''}" onclick="toggleSelect('${s._path}', event)">
        <input type="checkbox" class="session-checkbox" ${isSelected ? 'checked' : ''}
               onclick="event.stopPropagation(); toggleSelect('${s._path}')">
        <div class="session-info">
          <div class="session-name">${badge} ${escapeHtml(s.processName || '不明')}</div>
          <div class="session-meta">
            <span>モデル: ${escapeHtml(model)}</span>
            <span>作成: ${formatDate(s.createdAt)}</span>
            <span>最終: ${formatDate(s.lastActivityAt)}</span>
          </div>
          <div class="session-message">${escapeHtml(msg)}</div>
        </div>
        <div class="session-actions">
          ${s.isArchived ? `<button class="btn btn-success" onclick="event.stopPropagation(); restoreOne('${s._path}')">復元</button>` : ''}
          <button class="btn btn-danger" onclick="event.stopPropagation(); deleteOne('${s._path}')">削除</button>
        </div>
      </div>`;
  }).join('');

  updateBulkButtons();
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function toggleSelect(path, event) {
  if (selectedPaths.has(path)) {
    selectedPaths.delete(path);
  } else {
    selectedPaths.add(path);
  }
  renderSessions();
}

function toggleSelectAll(checked) {
  const filtered = getFiltered();
  if (checked) {
    filtered.forEach(s => selectedPaths.add(s._path));
  } else {
    filtered.forEach(s => selectedPaths.delete(s._path));
  }
  renderSessions();
}

function updateBulkButtons() {
  const hasSelection = selectedPaths.size > 0;
  document.getElementById('btn-restore-sel').disabled = !hasSelection;
  document.getElementById('btn-delete-sel').disabled = !hasSelection;
}

function showToast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast toast-' + type + ' show';
  setTimeout(() => t.classList.remove('show'), 3000);
}

function showModal(title, message, onConfirm, danger) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <h2>${title}</h2>
      <p>${message}</p>
      <div class="modal-actions">
        <button class="btn btn-ghost" id="modal-cancel">キャンセル</button>
        <button class="btn ${danger ? 'btn-danger' : 'btn-success'}" id="modal-confirm">実行</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('#modal-cancel').onclick = () => overlay.remove();
  overlay.querySelector('#modal-confirm').onclick = () => { overlay.remove(); onConfirm(); };
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
}

async function refresh() {
  const res = await api('list');
  sessions = res.sessions || [];
  selectedPaths.clear();
  document.getElementById('select-all').checked = false;
  renderSessions();
  showToast('セッション一覧を更新しました', 'success');
}

async function restoreOne(path) {
  const s = sessions.find(s => s._path === path);
  showModal('復元の確認',
    `「${escapeHtml(s.processName)}」を復元しますか？<br>反映には Claude Desktop の再起動 (Cmd+Q) が必要です。`,
    async () => {
      const res = await api('restore', { paths: [path] });
      if (res.success) {
        showToast('復元しました。Cmd+Q で再起動してください。', 'success');
        refresh();
      } else {
        showToast('復元に失敗しました', 'error');
      }
    });
}

async function deleteOne(path) {
  const s = sessions.find(s => s._path === path);
  showModal('削除の確認',
    `「${escapeHtml(s.processName)}」を完全に削除します。<br><strong>この操作は元に戻せません！</strong>`,
    async () => {
      const res = await api('delete', { paths: [path] });
      if (res.success) {
        showToast('削除しました', 'success');
        refresh();
      } else {
        showToast('削除に失敗しました', 'error');
      }
    }, true);
}

async function restoreSelected() {
  const paths = [...selectedPaths].filter(p => sessions.find(s => s._path === p && s.isArchived));
  if (!paths.length) { showToast('復元対象がありません', 'error'); return; }
  showModal('復元の確認',
    `${paths.length} 件を復元します。<br>反映には Claude Desktop の再起動が必要です。`,
    async () => {
      const res = await api('restore', { paths });
      showToast(`${res.count} 件を復元しました。Cmd+Q で再起動してください。`, 'success');
      refresh();
    });
}

async function deleteSelected() {
  const paths = [...selectedPaths];
  if (!paths.length) return;
  showModal('一括削除の確認',
    `${paths.length} 件を完全に削除します。<br><strong>この操作は元に戻せません！</strong>`,
    async () => {
      const res = await api('delete', { paths });
      showToast(`${res.count} 件を削除しました`, 'success');
      refresh();
    }, true);
}

async function restoreAllArchived() {
  const archived = sessions.filter(s => s.isArchived);
  if (!archived.length) { showToast('アーカイブ済みセッションはありません', 'error'); return; }
  showModal('一括復元の確認',
    `アーカイブ済みの ${archived.length} 件すべてを復元します。`,
    async () => {
      const res = await api('restore', { paths: archived.map(s => s._path) });
      showToast(`${res.count} 件を復元しました。Cmd+Q で再起動してください。`, 'success');
      refresh();
    });
}

async function deleteAllArchived() {
  const archived = sessions.filter(s => s.isArchived);
  if (!archived.length) { showToast('アーカイブ済みセッションはありません', 'error'); return; }
  showModal('全アーカイブ削除',
    `アーカイブ済みの ${archived.length} 件すべてを完全に削除します。<br><strong>この操作は元に戻せません！</strong>`,
    async () => {
      const res = await api('delete', { paths: archived.map(s => s._path) });
      showToast(`${res.count} 件を削除しました`, 'success');
      refresh();
    }, true);
}

async function openFolder() {
  await api('open_folder');
}

// 初期読み込み
refresh();
</script>
</body>
</html>
"""


# --- HTTP サーバー ---

class Handler(BaseHTTPRequestHandler):
    last_heartbeat = time.time()

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode("utf-8"))

    def do_POST(self):
        path = urlparse(self.path).path
        content_len = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_len)) if content_len > 0 else {}

        result = {}

        if path == "/api/heartbeat":
            Handler.last_heartbeat = time.time()
            result = {"ok": True}

        elif path == "/api/list":
            sessions = load_sessions()
            result = {"sessions": sessions}

        elif path == "/api/restore":
            paths = body.get("paths", [])
            count = sum(1 for p in paths if restore_session(p))
            result = {"success": True, "count": count}

        elif path == "/api/delete":
            paths = body.get("paths", [])
            count = sum(1 for p in paths if delete_session(p))
            result = {"success": True, "count": count}

        elif path == "/api/open_folder":
            sessions_dir = find_sessions_dir()
            if sessions_dir:
                system = platform.system()
                if system == "Darwin":
                    subprocess.run(["open", str(sessions_dir)])
                elif system == "Windows":
                    subprocess.run(["explorer", str(sessions_dir)])
                elif system == "Linux":
                    subprocess.run(["xdg-open", str(sessions_dir)])
            result = {"success": True}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


def watchdog(server):
    """ブラウザからのハートビートが途絶えたらサーバーを自動終了"""
    while True:
        time.sleep(5)
        elapsed = time.time() - Handler.last_heartbeat
        if elapsed > 6:
            print("ブラウザが閉じられたため、サーバーを終了します。")
            remove_lock()
            os._exit(0)


def main():
    # 既存サーバーが動いていたら、ブラウザだけ開いて終了
    if is_server_running():
        print(f"既存のサーバーが動作中です。ブラウザを開きます。")
        webbrowser.open(f"http://127.0.0.1:{PORT}")
        return

    # 古いプロセスが残っていれば終了
    kill_existing_server()

    try:
        server = HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"ポート {PORT} が使用中です。既存プロセスを終了して再試行します。")
        kill_existing_server()
        time.sleep(1)
        server = HTTPServer(("127.0.0.1", PORT), Handler)

    write_lock()
    url = f"http://127.0.0.1:{PORT}"

    print(f"Cowork Archive Manager v{VERSION}")
    print(f"サーバー起動: {url}")
    print("ブラウザタブを閉じると自動終了します。")

    # ウォッチドッグ（ブラウザ閉じたら自動終了）
    t = threading.Thread(target=watchdog, args=(server,), daemon=True)
    t.start()

    # ブラウザで開く
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    # Ctrl+C でクリーンに終了
    def shutdown(sig, frame):
        print("\nサーバーを停止します...")
        remove_lock()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server.serve_forever()


if __name__ == "__main__":
    main()
