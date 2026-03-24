# Cowork Archive Manager

Claude Desktop (Cowork) のアーカイブ済みセッションを管理するGUIツールです。

## なぜ必要？

Claude Desktop にはアーカイブしたタスクを表示する「Archived」セクションがありますが、既知のバグにより表示されない問題が報告されています（[#22931](https://github.com/anthropics/claude-code/issues/22931)、[#24534](https://github.com/anthropics/claude-code/issues/24534)）。また、セッションの削除機能も未実装です（[#25304](https://github.com/anthropics/claude-code/issues/25304)）。

このツールはバグが修正されるまでの回避策として、アーカイブ済みセッションの**復元**と**削除**をブラウザベースのGUIで行えるようにします。

## スクリーンショット

起動するとブラウザに管理画面が表示されます。セッションの一覧表示、フィルタリング、個別/一括の復元・削除が可能です。

## 機能

- セッション一覧の表示（アーカイブ済み / アクティブ / 全件）
- 個別または一括での復元（`isArchived` → `false`）
- 個別または一括での完全削除
- セッションフォルダをファイルマネージャで開く
- ブラウザタブを閉じると自動終了

## 動作環境

- **macOS** / **Windows** / **Linux**
- Python 3.8 以上（外部パッケージ不要）
- Claude Desktop がインストール済みであること

## インストール

### 方法1: Python スクリプトとして直接実行

```bash
git clone https://github.com/YOUR_USERNAME/cowork-archive-manager.git
cd cowork-archive-manager
python3 cowork_archive_manager.py
```

### 方法2: macOS アプリとしてインストール

```bash
git clone https://github.com/YOUR_USERNAME/cowork-archive-manager.git
cd cowork-archive-manager
chmod +x install.sh
./install.sh
```

`~/Applications/Cowork Archive Manager.app` が生成されます。ダブルクリックで起動できます。

> **Note:** Xcode Command Line Tools が必要です。未インストールの場合は `xcode-select --install` を実行してください。

## 使い方

1. ツールを起動すると、ブラウザに管理画面が自動で開きます
2. フィルターで「アーカイブ済み」を選択すると、アーカイブされたセッションが一覧表示されます
3. 「復元」ボタンでセッションを復元、「削除」ボタンで完全削除できます
4. 復元後は **Claude Desktop を完全終了（macOS: Cmd+Q）→ 再起動** で反映されます

## アンインストール

### macOS アプリ

```bash
rm -rf ~/Applications/Cowork\ Archive\ Manager.app
```

### ロックファイル

```bash
rm -f ~/.cowork_archive_manager.lock
```

## 技術的な仕組み

- Python の標準ライブラリのみを使用（`http.server`）
- `127.0.0.1:52849` でローカルサーバーを起動（外部通信なし）
- セッションファイル（JSON）の `isArchived` フラグを操作して復元
- ブラウザからのハートビートが途絶えると自動終了

### セッションファイルの場所

| OS | パス |
|---|---|
| macOS | `~/Library/Application Support/Claude/local-agent-mode-sessions/` |
| Windows | `%APPDATA%/Claude/local-agent-mode-sessions/` |
| Linux | `~/.config/Claude/local-agent-mode-sessions/` |

## ライセンス

MIT License - 詳細は [LICENSE](LICENSE) をご覧ください。
