#!/usr/bin/env python3
"""
Cowork Archive Manager
Claude Desktop (Cowork) のアーカイブ済みセッションを管理するGUIツール

起動すると localhost でサーバーが立ち上がり、ブラウザに管理画面が表示されます。
ブラウザタブを閉じると自動的にサーバーも終了します。

GitHub: https://github.com/SugaCrypto/cowork-archive-manager
License: MIT
"""

import argparse
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
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# --- 定数 ---
VERSION = "1.2.0"
APP_TITLE = "Cowork Archive Manager"
PORT = 52849
LOCK_FILE = Path.home() / ".cowork_archive_manager.lock"

# コマンドライン引数で指定されたカスタムパス（None = 自動検出）
custom_sessions_path = None

def get_candidate_paths():
    """OSに応じた候補パスのリストを返す（優先度順）"""
    system = platform.system()
    home = Path.home()
    candidates = []
    # Cowork セッション専用（local-agent-mode-sessions のみ）
    session_dir = "local-agent-mode-sessions"
    if system == "Darwin":
        candidates = [
            home / "Library" / "Application Support" / "Claude" / session_dir,
            home / "Library" / "Application Support" / "claude-desktop" / session_dir,
            home / ".claude" / session_dir,
        ]
    elif system == "Windows":
        appdata = Path(os.environ.get("APPDATA", ""))
        localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
        candidates = [
            appdata / "Claude" / session_dir,
        ]
        # MSIX版のパッケージディレクトリを動的に検出
        msix_base = localappdata / "Packages"
        if msix_base.exists():
            try:
                for pkg in msix_base.iterdir():
                    if pkg.is_dir() and pkg.name.startswith("Claude_"):
                        candidates.append(pkg / "LocalCache" / "Roaming" / "Claude" / session_dir)
            except PermissionError:
                pass
        candidates += [
            localappdata / "Claude" / session_dir,
            appdata / "claude-desktop" / session_dir,
            localappdata / "claude-desktop" / session_dir,
            home / ".claude" / session_dir,
        ]
    else:
        candidates = [
            home / ".config" / "Claude" / session_dir,
            home / ".config" / "claude-desktop" / session_dir,
            home / ".claude" / session_dir,
        ]
    return candidates



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

def _search_in_base(base_path):
    """指定ベースパス配下でセッションファイルを探す。見つかったら (パス, True) を、なければ (None, ディレクトリ存在有無) を返す"""
    if not base_path.exists():
        return None, False
    if any(iter_session_json_files(base_path)):
        return base_path, True
    return None, True


def iter_session_json_files(base_path):
    """消えた/アクセス不能なサブディレクトリを無視しながら local_*.json を列挙する"""
    try:
        base_path = Path(base_path)
    except TypeError:
        return

    if not base_path.exists():
        return

    def _on_walk_error(_err):
        # Claude Desktop 側で消えた作業ディレクトリやアクセス不能な場所はスキップする
        return

    for root, _, files in os.walk(base_path, onerror=_on_walk_error):
        for name in files:
            if name.startswith("local_") and name.endswith(".json"):
                yield Path(root) / name


def find_sessions_dir():
    """セッションディレクトリを探索し、(パス or None, 診断情報dict) を返す"""
    candidates = get_candidate_paths()
    diag = {
        "os": platform.system(),
        "custom_path": str(custom_sessions_path) if custom_sessions_path else None,
        "searched_paths": [str(p) for p in candidates],
        "searched_base": str(candidates[0]) if candidates else None,
        "base_exists": False,
        "subdirs_found": [],
        "reason": None,
    }

    # カスタムパスが指定されている場合はそれを使用
    if custom_sessions_path is not None:
        p = Path(custom_sessions_path)
        diag["searched_base"] = str(p)
        diag["searched_paths"] = [str(p)]
        if not p.exists():
            diag["reason"] = "custom_path_not_found"
            return None, diag
        if not p.is_dir():
            diag["reason"] = "custom_path_not_dir"
            return None, diag
        found, _ = _search_in_base(p)
        if found:
            diag["base_exists"] = True
            return found, diag
        diag["reason"] = "no_session_files_in_custom_path"
        return None, diag

    # 自動検出: 全候補パスを順に探索
    for candidate in candidates:
        found, exists = _search_in_base(candidate)
        if found:
            diag["searched_base"] = str(candidate)
            diag["base_exists"] = True
            return found, diag
        if exists:
            diag["base_exists"] = True

    diag["searched_base"] = str(candidates[0]) if candidates else "?"
    diag["reason"] = "base_dir_not_found" if not diag["base_exists"] else "no_session_files"
    return None, diag


def load_sessions():
    """全セッションと診断情報を返す"""
    sessions_dir, diag = find_sessions_dir()
    if sessions_dir is None:
        return [], diag
    diag["sessions_dir"] = str(sessions_dir)
    sessions = []
    for json_file in iter_session_json_files(sessions_dir):
        try:
            with open(json_file) as f:
                data = json.load(f)
            data["_path"] = str(json_file)
            sessions.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    sessions.sort(key=lambda s: s.get("lastActivityAt", 0), reverse=True)
    return sessions, diag


_cached_sessions_dir = None

def _validate_session_path(json_path):
    """セッションパスがセッションディレクトリ内にあることを検証"""
    global _cached_sessions_dir
    p = Path(json_path).resolve()
    if _cached_sessions_dir is None:
        sessions_dir, _ = find_sessions_dir()
        if sessions_dir is None:
            return False
        _cached_sessions_dir = sessions_dir.resolve()
    try:
        p.relative_to(_cached_sessions_dir)
        return True
    except ValueError:
        return False


def restore_session(json_path):
    """セッションを復元（isArchived を false に変更）"""
    if not _validate_session_path(json_path):
        return False
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
    if not _validate_session_path(json_path):
        return False
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300..700;1,9..40,300..700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0c0e14;
    --surface: #14171f;
    --surface2: #1c2029;
    --surface3: #242833;
    --accent: #e8643a;
    --accent-soft: rgba(232,100,58,0.12);
    --accent-hover: #d4572f;
    --emerald: #34d399;
    --emerald-soft: rgba(52,211,153,0.12);
    --amber: #fbbf24;
    --amber-soft: rgba(251,191,36,0.12);
    --rose: #f43f5e;
    --rose-soft: rgba(244,63,94,0.12);
    --text: #e8eaed;
    --text2: #7a8194;
    --text3: #4a5068;
    --border: rgba(255,255,255,0.06);
    --border-hover: rgba(255,255,255,0.12);
    --radius: 10px;
    --radius-lg: 14px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }

  /* --- Header --- */
  .header {
    padding: 20px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
  }
  .header-left { display: flex; align-items: center; gap: 14px; }
  .header-logo {
    width: 32px; height: 32px;
    border-radius: 8px;
    flex-shrink: 0;
    object-fit: cover;
  }
  .header h1 {
    font-size: 17px;
    font-weight: 600;
    color: var(--text);
    letter-spacing: -0.3px;
  }
  .header-version {
    font-size: 11px;
    color: var(--text3);
    font-weight: 400;
    margin-left: 2px;
  }
  .header-actions { display: flex; gap: 8px; }

  /* --- Buttons --- */
  .btn {
    padding: 7px 14px;
    border: none;
    border-radius: var(--radius);
    cursor: pointer;
    font-family: inherit;
    font-size: 13px;
    font-weight: 500;
    transition: background-color 0.15s ease, transform 0.1s ease, opacity 0.15s ease;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    letter-spacing: -0.1px;
  }
  .btn:hover { transform: translateY(-1px); }
  .btn:active { transform: translateY(0); }
  .btn-accent { background: var(--accent); color: white; }
  .btn-accent:hover { background: var(--accent-hover); }
  .btn-emerald { background: var(--emerald); color: #0c0e14; }
  .btn-emerald:hover { background: #2bc48d; }
  .btn-amber { background: var(--amber); color: #0c0e14; }
  .btn-amber:hover { background: #e5ac1e; }
  .btn-rose { background: var(--rose); color: white; }
  .btn-rose:hover { background: #dc3550; }
  .btn-ghost {
    background: var(--surface2);
    color: var(--text2);
    border: 1px solid var(--border);
  }
  .btn-ghost:hover { background: var(--surface3); color: var(--text); border-color: var(--border-hover); }
  .btn:disabled { opacity: 0.35; cursor: not-allowed; transform: none; }
  .btn-sm { padding: 5px 10px; font-size: 12px; }

  /* --- Toolbar --- */
  .toolbar {
    padding: 16px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }
  .toolbar-left { display: flex; align-items: center; gap: 12px; }
  .filter-group {
    display: flex;
    gap: 2px;
    background: var(--surface);
    border-radius: var(--radius);
    padding: 3px;
    border: 1px solid var(--border);
  }
  .filter-btn {
    padding: 6px 16px;
    border: none;
    border-radius: 7px;
    background: transparent;
    color: var(--text2);
    cursor: pointer;
    font-family: inherit;
    font-size: 13px;
    font-weight: 500;
    transition: background-color 0.15s ease, color 0.15s ease;
  }
  .filter-btn:hover { color: var(--text); }
  .filter-btn.active {
    background: var(--surface3);
    color: var(--text);
    box-shadow: 0 1px 3px rgba(0,0,0,0.3);
  }
  .count-badge {
    font-size: 12px;
    font-weight: 500;
    color: var(--text3);
    font-variant-numeric: tabular-nums;
  }

  /* --- Bulk Actions --- */
  .bulk-actions {
    padding: 4px 32px 12px;
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: center;
  }
  .bulk-divider {
    width: 1px;
    height: 20px;
    background: var(--border);
    margin: 0 4px;
  }

  /* --- Select All --- */
  .select-all-area {
    padding: 0 32px 8px;
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13px;
    color: var(--text2);
  }

  /* --- Custom Checkbox --- */
  .ck {
    appearance: none;
    -webkit-appearance: none;
    width: 18px; height: 18px;
    border: 2px solid var(--text3);
    border-radius: 5px;
    cursor: pointer;
    transition: background-color 0.15s ease, border-color 0.15s ease;
    position: relative;
    flex-shrink: 0;
  }
  .ck:checked {
    background: var(--accent);
    border-color: var(--accent);
  }
  .ck:checked::after {
    content: '';
    position: absolute;
    left: 4px; top: 1px;
    width: 5px; height: 9px;
    border: solid white; border-width: 0 2px 2px 0;
    transform: rotate(45deg);
  }
  .ck:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  /* --- Session List --- */
  .session-list {
    padding: 0 32px 32px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .session-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 14px 18px;
    display: grid;
    grid-template-columns: 28px 1fr auto;
    gap: 14px;
    align-items: center;
    transition: background-color 0.15s ease, border-color 0.15s ease;
    cursor: pointer;
    animation: cardIn 0.3s ease both;
  }
  .session-card:hover {
    border-color: var(--border-hover);
    background: var(--surface2);
  }
  .session-card.selected {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  @keyframes cardIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .session-info { min-width: 0; }
  .session-name {
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 4px;
    display: flex;
    align-items: center;
    gap: 8px;
    letter-spacing: -0.2px;
  }
  .session-meta {
    font-size: 12px;
    color: var(--text2);
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 3px;
    font-variant-numeric: tabular-nums;
  }
  .session-message {
    font-size: 12px;
    color: var(--text3);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 640px;
    font-family: 'JetBrains Mono', monospace;
    font-weight: 400;
  }
  .badge {
    font-size: 10px;
    padding: 2px 7px;
    border-radius: 5px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .badge-archived { background: var(--amber-soft); color: var(--amber); }
  .badge-active { background: var(--emerald-soft); color: var(--emerald); }
  .session-actions {
    display: flex;
    gap: 4px;
    flex-shrink: 0;
    opacity: 0.5;
    transition: opacity 0.15s ease;
  }
  .session-card:hover .session-actions { opacity: 1; }

  /* --- Toast --- */
  .toast {
    position: fixed;
    bottom: 28px;
    left: 50%;
    transform: translateX(-50%) translateY(80px);
    padding: 10px 20px;
    border-radius: var(--radius);
    font-size: 13px;
    font-weight: 500;
    z-index: 1000;
    transition: transform 0.25s cubic-bezier(0.16,1,0.3,1);
    max-width: 90vw;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }
  .toast.show { transform: translateX(-50%) translateY(0); }
  .toast-success { background: rgba(52,211,153,0.9); color: #0c0e14; }
  .toast-error { background: rgba(244,63,94,0.9); color: white; }

  /* --- Modal --- */
  .modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.65);
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 200;
    animation: fadeIn 0.15s ease;
  }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
  .modal {
    background: var(--surface2);
    border: 1px solid var(--border-hover);
    border-radius: var(--radius-lg);
    padding: 24px;
    max-width: 440px;
    width: 90%;
    animation: modalIn 0.2s cubic-bezier(0.16,1,0.3,1);
  }
  @keyframes modalIn {
    from { opacity: 0; transform: scale(0.96) translateY(8px); }
    to { opacity: 1; transform: scale(1) translateY(0); }
  }
  .modal h2 { font-size: 16px; margin-bottom: 10px; font-weight: 600; letter-spacing: -0.2px; }
  .modal p { color: var(--text2); margin-bottom: 18px; line-height: 1.7; font-size: 13px; }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; }

  /* --- Empty State --- */
  .empty-state {
    text-align: center;
    padding: 80px 32px;
    color: var(--text2);
  }
  .empty-icon {
    width: 56px; height: 56px;
    margin: 0 auto 20px;
    background: var(--surface2);
    border-radius: 14px;
    display: flex; align-items: center; justify-content: center;
    font-size: 24px;
  }
  .empty-state p { font-size: 14px; line-height: 1.8; }
  .empty-state code {
    background: var(--surface2);
    padding: 2px 6px;
    border-radius: 4px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
  }

  /* --- Path Input --- */
  .path-input-area {
    margin: 20px auto 0;
    display: flex;
    gap: 6px;
    align-items: center;
    max-width: 560px;
  }
  .path-input-area input {
    flex: 1;
    padding: 9px 14px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--surface);
    color: var(--text);
    font-size: 13px;
    font-family: 'JetBrains Mono', monospace;
    transition: border-color 0.15s ease;
  }
  .path-input-area input::placeholder { color: var(--text3); }
  .path-input-area input:focus { outline: none; border-color: var(--accent); }
  .candidate-paths {
    margin: 16px auto 0;
    text-align: left;
    max-width: 560px;
  }
  .candidate-paths p {
    font-size: 12px;
    margin-bottom: 8px;
    color: var(--text3);
  }
  .candidate-path-btn {
    display: block;
    width: 100%;
    text-align: left;
    padding: 8px 12px;
    margin-bottom: 4px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface);
    color: var(--amber);
    font-size: 11px;
    font-family: 'JetBrains Mono', monospace;
    cursor: pointer;
    transition: background-color 0.15s ease, border-color 0.15s ease;
    word-break: break-all;
  }
  .candidate-path-btn:hover {
    border-color: var(--accent);
    background: var(--surface2);
  }

  /* --- Responsive --- */
  @media (max-width: 640px) {
    .header, .toolbar, .bulk-actions, .select-all-area, .session-list { padding-left: 16px; padding-right: 16px; }
    .session-card { grid-template-columns: 24px 1fr; }
    .session-actions { display: none; }
  }

  /* --- Reduced Motion --- */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: 0.01ms !important;
      transition-duration: 0.01ms !important;
    }
  }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <img class="header-logo" src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMjUgMTI1IiB3aWR0aD0iNTEyIiBoZWlnaHQ9IjUxMiI+CiAgPHJlY3Qgd2lkdGg9IjEyNSIgaGVpZ2h0PSIxMjUiIHJ4PSIyOCIgZmlsbD0iIzFhMWExYSIvPgogIDxwYXRoIGQ9Ik01NC4zNzUgMTE4Ljc1TDU2LjEyNSAxMTFMNTguMTI1IDEwMUw1OS43NSA5M0w2MS4yNSA4My4xMjVMNjIuMTI1IDc5Ljg3NUw2MiA3OS42MjVMNjEuMzc1IDc5Ljc1TDUzLjg3NSA5MEw0Mi41IDEwNS4zNzVMMzMuNSAxMTQuODc1TDMxLjM3NSAxMTUuNzVMMjcuNjI1IDExMy44NzVMMjggMTEwLjM3NUwzMC4xMjUgMTA3LjM3NUw0Mi41IDkxLjVMNTAgODEuNjI1TDU0Ljg3NSA3Nkw1NC43NSA3NS4yNUg1NC41TDIxLjUgOTYuNzVMMTUuNjI1IDk3LjVMMTMgOTUuMTI1TDEzLjM3NSA5MS4yNUwxNC42MjUgOTBMMjQuNSA4My4xMjVMNDkuMTI1IDY5LjM3NUw0OS41IDY4LjEyNUw0OS4xMjUgNjcuNUg0Ny44NzVMNDMuNzUgNjcuMjVMMjkuNzUgNjYuODc1TDE3LjYyNSA2Ni4zNzVMNS43NSA2NS43NUwyLjc1IDY1LjEyNUwwIDYxLjM3NUwwLjI1IDU5LjVMMi43NSA1Ny44NzVMNi4zNzUgNTguMTI1TDE0LjI1IDU4Ljc1TDI2LjEyNSA1OS41TDM0Ljc1IDYwTDQ3LjUgNjEuMzc1SDQ5LjVMNDkuNzUgNjAuNUw0OS4xMjUgNjBMNDguNjI1IDU5LjVMMzYuMjUgNTEuMjVMMjMgNDIuNUwxNiAzNy4zNzVMMTIuMjUgMzQuNzVMMTAuMzc1IDMyLjM3NUw5LjYyNSAyNy4xMjVMMTMgMjMuMzc1TDE3LjYyNSAyMy43NUwxOC43NSAyNEwyMy4zNzUgMjcuNjI1TDMzLjI1IDM1LjI1TDQ2LjI1IDQ0Ljg3NUw0OC4xMjUgNDYuMzc1TDQ5IDQ1Ljg3NVY0NS41TDQ4LjEyNSA0NC4xMjVMNDEuMTI1IDMxLjM3NUwzMy42MjUgMTguMzc1TDMwLjI1IDEzTDI5LjM3NSA5Ljc1QzI5LjA0MTcgOC42MjUgMjguODc1IDcuMzc1IDI4Ljg3NSA2TDMyLjc1IDAuNzUwMDA2TDM0Ljg3NSAwTDQwLjEyNSAwLjc1MDAwNkw0Mi4yNSAyLjYyNUw0NS41IDEwTDUwLjYyNSAyMS42MjVMNTguNzUgMzcuMzc1TDYxLjEyNSA0Mi4xMjVMNjIuMzc1IDQ2LjM3NUw2Mi44NzUgNDcuNzVINjMuNzVWNDdMNjQuMzc1IDM4TDY1LjYyNSAyNy4xMjVMNjYuODc1IDEzLjEyNUw2Ny4yNSA5LjEyNUw2OS4yNSA0LjM3NUw3My4xMjUgMS44NzUwMUw3Ni4xMjUgMy4yNUw3OC42MjUgNi44NzVMNzguMjUgOS4xMjVMNzYuODc1IDE4Ljc1TDczLjg3NSAzMy44NzVMNzIgNDQuMTI1SDczLjEyNUw3NC4zNzUgNDIuNzVMNzkuNSAzNkw4OC4xMjUgMjUuMjVMOTEuODc1IDIxTDk2LjM3NSAxNi4yNUw5OS4yNSAxNEgxMDQuNjI1TDEwOC41IDE5Ljg3NUwxMDYuNzUgMjZMMTAxLjI1IDMzTDk2LjYyNSAzOC44NzVMOTAgNDcuNzVMODYgNTQuODc1TDg2LjM3NSA1NS4zNzVIODcuMjVMMTAyLjEyNSA1Mi4xMjVMMTEwLjI1IDUwLjc1TDExOS43NSA0OS4xMjVMMTI0LjEyNSA1MS4xMjVMMTI0LjYyNSA1My4xMjVMMTIyLjg3NSA1Ny4zNzVMMTEyLjYyNSA1OS44NzVMMTAwLjYyNSA2Mi4yNUw4Mi43NSA2Ni41TDgyLjUgNjYuNjI1TDgyLjc1IDY3TDkwLjc1IDY3Ljc1TDk0LjI1IDY4SDEwMi43NUwxMTguNSA2OS4xMjVMMTIyLjYyNSA3MS44NzVMMTI1IDc1LjEyNUwxMjQuNjI1IDc3Ljc1TDExOC4yNSA4MC44NzVMMTA5Ljc1IDc4Ljg3NUw4OS43NSA3NC4xMjVMODMgNzIuNUg4MlY3M0w4Ny43NSA3OC42MjVMOTguMTI1IDg4TDExMS4yNSAxMDAuMTI1TDExMS44NzUgMTAzLjEyNUwxMTAuMjUgMTA1LjYyNUwxMDguNSAxMDUuMzc1TDk3IDk2LjYyNUw5Mi41IDkyLjc1TDgyLjUgODQuMzc1SDgxLjg3NVY4NS4yNUw4NC4xMjUgODguNjI1TDk2LjM3NSAxMDdMOTcgMTEyLjYyNUw5Ni4xMjUgMTE0LjM3NUw5Mi44NzUgMTE1LjVMODkuNSAxMTQuODc1TDgyLjI1IDEwNC44NzVMNzQuODc1IDkzLjVMNjguODc1IDgzLjM3NUw2OC4yNSA4My44NzVMNjQuNjI1IDEyMS42MjVMNjMgMTIzLjVMNTkuMjUgMTI1TDU2LjEyNSAxMjIuNjI1TDU0LjM3NSAxMTguNzVaIiBmaWxsPSIjRThEREQzIi8+Cjwvc3ZnPgo=" alt="Cowork Archive Manager logo" aria-hidden="true">
    <h1>Cowork Archive Manager <span class="header-version">v""" + VERSION + r"""</span></h1>
  </div>
  <div class="header-actions">
    <button class="btn btn-ghost btn-sm" onclick="openFolder()" aria-label="Open session folder">フォルダを開く</button>
    <button class="btn btn-accent btn-sm" onclick="refresh()" aria-label="Refresh sessions">更新</button>
  </div>
</div>

<div class="toolbar">
  <div class="toolbar-left">
    <div class="filter-group" role="tablist">
      <button class="filter-btn active" role="tab" data-filter="all" onclick="setFilter('all')">すべて</button>
      <button class="filter-btn" role="tab" data-filter="archived" onclick="setFilter('archived')">アーカイブ済み</button>
      <button class="filter-btn" role="tab" data-filter="active" onclick="setFilter('active')">アクティブ</button>
    </div>
    <span class="count-badge" id="count"></span>
  </div>
</div>

<div class="bulk-actions">
  <button class="btn btn-emerald btn-sm" onclick="restoreSelected()" id="btn-restore-sel" disabled>選択を復元</button>
  <button class="btn btn-rose btn-sm" onclick="deleteSelected()" id="btn-delete-sel" disabled>選択を削除</button>
  <span class="bulk-divider"></span>
  <button class="btn btn-ghost btn-sm" onclick="restoreAllArchived()">全アーカイブを復元</button>
  <button class="btn btn-ghost btn-sm" onclick="deleteAllArchived()">全アーカイブを削除</button>
</div>

<div class="select-all-area">
  <input type="checkbox" id="select-all" class="ck" onchange="toggleSelectAll(this.checked)">
  <label for="select-all">すべて選択</label>
</div>

<div class="session-list" id="session-list"></div>

<div class="toast" id="toast"></div>

<script>
let sessions = [];
let currentFilter = 'all';
let selectedPaths = new Set();
let lastDiagnostic = null;

// --- i18n ---
const isJa = navigator.language.startsWith('ja');
const _isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
const _restartHint = _isMac ? 'Cmd+Q' : 'Alt+F4';
const i18n = {
  openFolder:       isJa ? 'フォルダを開く'                   : 'Open Folder',
  refresh:          isJa ? '更新'                             : 'Refresh',
  all:              isJa ? 'すべて'                           : 'All',
  archived:         isJa ? 'アーカイブ済み'                    : 'Archived',
  active:           isJa ? 'アクティブ'                        : 'Active',
  restoreSelected:  isJa ? '選択を復元'                       : 'Restore Selected',
  deleteSelected:   isJa ? '選択を削除'                       : 'Delete Selected',
  restoreAll:       isJa ? '全アーカイブを復元'                : 'Restore All Archived',
  deleteAll:        isJa ? '全アーカイブを削除'                : 'Delete All Archived',
  selectAll:        isJa ? 'すべて選択'                       : 'Select All',
  restore:          isJa ? '復元'                             : 'Restore',
  delete_:          isJa ? '削除'                             : 'Delete',
  cancel:           isJa ? 'キャンセル'                        : 'Cancel',
  execute:          isJa ? '実行'                             : 'Confirm',
  unknown:          isJa ? '不明'                             : 'Unknown',
  model:            isJa ? 'モデル'                           : 'Model',
  created:          isJa ? '作成'                             : 'Created',
  lastActive:       isJa ? '最終'                             : 'Last Active',
  badgeArchived:    isJa ? 'アーカイブ'                        : 'Archived',
  badgeActive:      isJa ? 'アクティブ'                        : 'Active',
  items:            isJa ? '件'                               : 'items',
  empty:            isJa ? '該当するセッションはありません'       : 'No sessions found',
  diagBaseNotFound: isJa
    ? (p) => `セッションディレクトリが見つかりません<br><code style="font-size:12px;color:#f0a500;word-break:break-all">${p}</code><br><br>Claude Desktop がインストールされ、エージェントモードでセッションを実行した履歴があることを確認してください。<br>または <code>--path</code> オプションでディレクトリを手動指定できます。`
    : (p) => `Session directory not found<br><code style="font-size:12px;color:#f0a500;word-break:break-all">${p}</code><br><br>Make sure Claude Desktop is installed and you have run at least one agent mode session.<br>Or specify a custom path with the <code>--path</code> option.`,
  diagNoFiles: isJa
    ? (p) => `セッションファイル (local_*.json) が見つかりません<br><code style="font-size:12px;color:#f0a500;word-break:break-all">${p}</code><br><br>Claude Desktop でエージェントモードのセッションを実行してからお試しください。`
    : (p) => `No session files (local_*.json) found in<br><code style="font-size:12px;color:#f0a500;word-break:break-all">${p}</code><br><br>Run an agent mode session in Claude Desktop first.`,
  diagCustomNotFound: isJa
    ? (p) => `指定されたパスが存在しません<br><code style="font-size:12px;color:#f0a500;word-break:break-all">${p}</code><br><br>パスを確認して再度お試しください。`
    : (p) => `Specified path does not exist<br><code style="font-size:12px;color:#f0a500;word-break:break-all">${p}</code><br><br>Please check the path and try again.`,
  pathInputPlaceholder: isJa ? 'セッションディレクトリのパスを入力...' : 'Enter session directory path...',
  pathApply:         isJa ? '適用'                               : 'Apply',
  pathReset:         isJa ? '自動検出に戻す'                      : 'Reset to auto-detect',
  candidatePathsLabel: isJa ? '探索済みパス（クリックで適用）:'    : 'Searched paths (click to apply):',
  refreshed:        isJa ? 'セッション一覧を更新しました'        : 'Session list refreshed',
  noArchived:       isJa ? 'アーカイブ済みセッションはありません' : 'No archived sessions',
  noRestoreTarget:  isJa ? '復元対象がありません'               : 'No sessions to restore',
  restoreTitle:     isJa ? '復元の確認'                       : 'Confirm Restore',
  deleteTitle:      isJa ? '削除の確認'                       : 'Confirm Delete',
  bulkDeleteTitle:  isJa ? '一括削除の確認'                    : 'Confirm Bulk Delete',
  bulkRestoreTitle: isJa ? '一括復元の確認'                    : 'Confirm Bulk Restore',
  allArchiveDelete: isJa ? '全アーカイブ削除'                  : 'Delete All Archived',
  restoreMsg:       (name) => isJa
    ? `「${name}」を復元しますか？<br>反映には Claude Desktop の再起動 (${_restartHint}) が必要です。`
    : `Restore "${name}"?<br>Restart Claude Desktop (${_restartHint}) to apply changes.`,
  deleteMsg:        (name) => isJa
    ? `「${name}」を完全に削除します。<br><strong>この操作は元に戻せません！</strong>`
    : `Permanently delete "${name}".<br><strong>This cannot be undone!</strong>`,
  restoredOk:       isJa ? `復元しました。${_restartHint} で再起動してください。`  : `Restored. Restart Claude Desktop (${_restartHint}) to apply.`,
  restoreFail:      isJa ? '復元に失敗しました'                         : 'Failed to restore',
  deletedOk:        isJa ? '削除しました'                               : 'Deleted',
  deleteFail:       isJa ? '削除に失敗しました'                          : 'Failed to delete',
  bulkRestoreMsg:   (n) => isJa
    ? `${n} 件を復元します。<br>反映には Claude Desktop の再起動 (${_restartHint}) が必要です。`
    : `Restore ${n} session(s).<br>Restart Claude Desktop (${_restartHint}) to apply changes.`,
  bulkDeleteMsg:    (n) => isJa
    ? `${n} 件を完全に削除します。<br><strong>この操作は元に戻せません！</strong>`
    : `Permanently delete ${n} session(s).<br><strong>This cannot be undone!</strong>`,
  bulkRestoredOk:   (n) => isJa
    ? `${n} 件を復元しました。${_restartHint} で再起動してください。`
    : `Restored ${n} session(s). Restart Claude Desktop (${_restartHint}) to apply.`,
  bulkDeletedOk:    (n) => isJa ? `${n} 件を削除しました` : `Deleted ${n} session(s)`,
  allRestoreMsg:    (n) => isJa
    ? `アーカイブ済みの ${n} 件すべてを復元します。`
    : `Restore all ${n} archived session(s).`,
  allDeleteMsg:     (n) => isJa
    ? `アーカイブ済みの ${n} 件すべてを完全に削除します。<br><strong>この操作は元に戻せません！</strong>`
    : `Permanently delete all ${n} archived session(s).<br><strong>This cannot be undone!</strong>`,
};

// Apply i18n to static elements
document.addEventListener('DOMContentLoaded', () => {
  document.querySelector('[onclick="openFolder()"]').textContent = i18n.openFolder;
  document.querySelector('[onclick="refresh()"]').textContent = i18n.refresh;
  document.querySelector('[data-filter="all"]').textContent = i18n.all;
  document.querySelector('[data-filter="archived"]').textContent = i18n.archived;
  document.querySelector('[data-filter="active"]').textContent = i18n.active;
  document.getElementById('btn-restore-sel').textContent = i18n.restoreSelected;
  document.getElementById('btn-delete-sel').textContent = i18n.deleteSelected;
  document.querySelector('[onclick="restoreAllArchived()"]').textContent = i18n.restoreAll;
  document.querySelector('[onclick="deleteAllArchived()"]').textContent = i18n.deleteAll;
  document.querySelector('label[for="select-all"]').textContent = i18n.selectAll;
});

// Heartbeat
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
  if (!ms) return i18n.unknown;
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
  document.getElementById('count').textContent = filtered.length + ' ' + i18n.items;



  if (filtered.length === 0) {
    let emptyMsg = i18n.empty;
    let showPathInput = false;
    // セッション自体が0件で診断情報がある場合は詳細を表示
    if (sessions.length === 0 && lastDiagnostic) {
      const d = lastDiagnostic;
      const basePath = d.searched_base || '?';
      showPathInput = true;
      if (d.reason === 'base_dir_not_found') {
        emptyMsg = i18n.diagBaseNotFound(basePath);
      } else if (d.reason === 'no_session_files') {
        emptyMsg = i18n.diagNoFiles(basePath);
      } else if (d.reason === 'custom_path_not_found' || d.reason === 'custom_path_not_dir') {
        emptyMsg = i18n.diagCustomNotFound(d.custom_path || basePath);
      } else if (d.reason === 'no_session_files_in_custom_path') {
        emptyMsg = i18n.diagNoFiles(d.custom_path || basePath);
      }
    }

    let pathInputHtml = '';
    if (showPathInput && lastDiagnostic) {
      const paths = lastDiagnostic.searched_paths || [];
      const candidateList = paths.map((p, i) =>
        `<button class="candidate-path-btn" data-path-index="${i}" onclick="applyPath(lastDiagnostic.searched_paths[${i}])">${escapeHtml(p)}</button>`
      ).join('');
      pathInputHtml = `
        <div class="path-input-area">
          <input type="text" id="custom-path-input" placeholder="${i18n.pathInputPlaceholder}"
                 value="${escapeHtml(lastDiagnostic.custom_path || '')}"
                 onkeydown="if(event.key==='Enter')applyPathFromInput()">
          <button class="btn btn-accent btn-sm" onclick="applyPathFromInput()">${i18n.pathApply}</button>
          <button class="btn btn-ghost btn-sm" onclick="resetPath()">${i18n.pathReset}</button>
        </div>
        ${paths.length > 0 ? `<div class="candidate-paths">
          <p>${i18n.candidatePathsLabel}</p>
          ${candidateList}
        </div>` : ''}`;
    }

    list.innerHTML = `<div class="empty-state"><p>${emptyMsg}</p>${pathInputHtml}</div>`;
    return;
  }

  list.innerHTML = filtered.map((s, idx) => {
    const isSelected = selectedPaths.has(s._path);
    const badge = s.isArchived
      ? `<span class="badge badge-archived">${i18n.badgeArchived}</span>`
      : `<span class="badge badge-active">${i18n.badgeActive}</span>`;
    const msg = (s.initialMessage || '').replace(/\n/g, ' ').substring(0, 80);
    const model = s.model || i18n.unknown;
    return `
      <div class="session-card ${isSelected ? 'selected' : ''}" onclick="toggleSelectIdx(${idx}, event)" style="animation-delay:${idx * 30}ms">
        <input type="checkbox" class="ck" ${isSelected ? 'checked' : ''}
               onclick="event.stopPropagation(); toggleSelectIdx(${idx})">
        <div class="session-info">
          <div class="session-name">${badge} ${escapeHtml(s.processName || i18n.unknown)}</div>
          <div class="session-meta">
            <span>${i18n.model}: ${escapeHtml(model)}</span>
            <span>${i18n.created}: ${formatDate(s.createdAt)}</span>
            <span>${i18n.lastActive}: ${formatDate(s.lastActivityAt)}</span>
          </div>
          <div class="session-message">${escapeHtml(msg)}</div>
        </div>
        <div class="session-actions">
          ${s.isArchived ? `<button class="btn btn-emerald btn-sm" onclick="event.stopPropagation(); restoreOne(getFiltered()[${idx}]._path)">${i18n.restore}</button>` : ''}
          <button class="btn btn-rose btn-sm" onclick="event.stopPropagation(); deleteOne(getFiltered()[${idx}]._path)">${i18n.delete_}</button>
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


function toggleSelectIdx(idx, event) {
  const path = getFiltered()[idx]._path;
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
        <button class="btn btn-ghost" id="modal-cancel">${i18n.cancel}</button>
        <button class="btn ${danger ? 'btn-rose' : 'btn-emerald'}" id="modal-confirm">${i18n.execute}</button>
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
  lastDiagnostic = res.diagnostic || null;
  selectedPaths.clear();
  document.getElementById('select-all').checked = false;
  renderSessions();
  showToast(i18n.refreshed, 'success');
}

async function restoreOne(path) {
  const s = sessions.find(s => s._path === path);
  showModal(i18n.restoreTitle, i18n.restoreMsg(escapeHtml(s.processName)),
    async () => {
      const res = await api('restore', { paths: [path] });
      if (res.success) {
        showToast(i18n.restoredOk, 'success');
        refresh();
      } else {
        showToast(i18n.restoreFail, 'error');
      }
    });
}

async function deleteOne(path) {
  const s = sessions.find(s => s._path === path);
  showModal(i18n.deleteTitle, i18n.deleteMsg(escapeHtml(s.processName)),
    async () => {
      const res = await api('delete', { paths: [path] });
      if (res.success) {
        showToast(i18n.deletedOk, 'success');
        refresh();
      } else {
        showToast(i18n.deleteFail, 'error');
      }
    }, true);
}

async function restoreSelected() {
  const paths = [...selectedPaths].filter(p => sessions.find(s => s._path === p && s.isArchived));
  if (!paths.length) { showToast(i18n.noRestoreTarget, 'error'); return; }
  showModal(i18n.bulkRestoreTitle, i18n.bulkRestoreMsg(paths.length),
    async () => {
      const res = await api('restore', { paths });
      showToast(i18n.bulkRestoredOk(res.count), 'success');
      refresh();
    });
}

async function deleteSelected() {
  const paths = [...selectedPaths];
  if (!paths.length) return;
  showModal(i18n.bulkDeleteTitle, i18n.bulkDeleteMsg(paths.length),
    async () => {
      const res = await api('delete', { paths });
      showToast(i18n.bulkDeletedOk(res.count), 'success');
      refresh();
    }, true);
}

async function restoreAllArchived() {
  const archived = sessions.filter(s => s.isArchived);
  if (!archived.length) { showToast(i18n.noArchived, 'error'); return; }
  showModal(i18n.bulkRestoreTitle, i18n.allRestoreMsg(archived.length),
    async () => {
      const res = await api('restore', { paths: archived.map(s => s._path) });
      showToast(i18n.bulkRestoredOk(res.count), 'success');
      refresh();
    });
}

async function deleteAllArchived() {
  const archived = sessions.filter(s => s.isArchived);
  if (!archived.length) { showToast(i18n.noArchived, 'error'); return; }
  showModal(i18n.allArchiveDelete, i18n.allDeleteMsg(archived.length),
    async () => {
      const res = await api('delete', { paths: archived.map(s => s._path) });
      showToast(i18n.bulkDeletedOk(res.count), 'success');
      refresh();
    }, true);
}

async function applyPath(p) {
  const res = await api('set_path', { path: p });
  sessions = res.sessions || [];
  lastDiagnostic = res.diagnostic || null;
  selectedPaths.clear();
  document.getElementById('select-all').checked = false;
  renderSessions();
  if (sessions.length > 0) {
    showToast(isJa ? 'パスを適用しました' : 'Path applied', 'success');
  }
}

async function applyPathFromInput() {
  const input = document.getElementById('custom-path-input');
  if (input && input.value.trim()) {
    await applyPath(input.value.trim());
  }
}

async function resetPath() {
  await applyPath('');
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
        try:
            body = json.loads(self.rfile.read(content_len)) if content_len > 0 else {}
        except (json.JSONDecodeError, ValueError):
            body = {}

        result = {}

        if path == "/api/heartbeat":
            Handler.last_heartbeat = time.time()
            result = {"ok": True}

        elif path == "/api/list":
            sessions, diag = load_sessions()
            result = {"sessions": sessions, "diagnostic": diag}

        elif path == "/api/restore":
            paths = body.get("paths", [])
            count = sum(1 for p in paths if restore_session(p))
            result = {"success": True, "count": count}

        elif path == "/api/delete":
            paths = body.get("paths", [])
            count = sum(1 for p in paths if delete_session(p))
            result = {"success": True, "count": count}

        elif path == "/api/set_path":
            global custom_sessions_path, _cached_sessions_dir
            new_path = body.get("path", "").strip()
            if new_path:
                custom_sessions_path = new_path
            else:
                custom_sessions_path = None
            _cached_sessions_dir = None
            sessions, diag = load_sessions()
            result = {"success": True, "sessions": sessions, "diagnostic": diag}

        elif path == "/api/open_folder":
            sessions_dir, _ = find_sessions_dir()
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
    global custom_sessions_path

    parser = argparse.ArgumentParser(description="Cowork Archive Manager - Claude Desktop セッション管理ツール")
    parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="セッションディレクトリのパスを手動指定（例: --path \"C:\\Users\\you\\AppData\\Roaming\\Claude\\local-agent-mode-sessions\"）"
    )
    args = parser.parse_args()

    if args.path:
        custom_sessions_path = args.path
        print(f"カスタムパス指定: {custom_sessions_path}")

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
