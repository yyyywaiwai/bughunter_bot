# Bughunterbot (Discord Forum -> Claude Agent SDK -> PR)

Discordフォーラムの新規スレッドを検知し、対象リポジトリを `git pull` した後に Claude Agent SDK で原因/仕様/修正や新機能・改善提案を生成し、PRを作成してスレッドに投稿するボットです。

## 構成
- `main.py`: Discordボット本体
- `claude_runner.py`: Claude Agent SDK 呼び出し
- `repo_ops.py`: git/gh 操作
- `storage.py`: ジョブ管理SQLite
- `config.py`: 設定読み込み

## セットアップ
1. 依存関係をインストール

```bash
pip install -r requirements.txt
```

2. `.env` を作成（`.env.example` 参照）

必須:
- `DISCORD_BOT_TOKEN`
- `OWNER_IDS`
- `FORUM_REPO_MAP`
- `ANTHROPIC_API_KEY`

推奨:
- `DISCORD_GUILD_ID`（スラッシュコマンド同期を高速化）
- `GH_TOKEN` もしくは `GITHUB_TOKEN`（PR作成用）
- （任意・非公式）`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`（サードパーティAPI向け）

3. 対象リポジトリを `repos/` 配下にクローン

```bash
mkdir -p repos
git clone git@github.com:your_org/your_repo.git repos/your_repo
```

4. 起動

```bash
python main.py
```

## 対象リポジトリの準備
- `repos/` 配下に **対象リポジトリを事前にクローン** しておく必要があります。
- `.env` の `FORUM_REPO_MAP` は `repos/` 内のパスに合わせてください。
- private リポジトリの場合、`git pull`/`git push` と `gh pr create` の両方に認証が必要です。
  - `git` は SSH でもOKですが、`gh` は `GH_TOKEN`/`GITHUB_TOKEN` を使います。
  - fine-grained PAT を使う場合は対象リポジトリにアクセス権を付与してください。

## FORUM_REPO_MAP の設定方法
`FORUM_REPO_MAP` は「フォーラムチャンネルID -> リポジトリパス」の JSON です。

例:
```
FORUM_REPO_MAP={"111111111111111111":"./repos/iMonos","222222222222222222":"./repos/IIJWidget"}
```

- キー: フォーラムチャンネルID（DiscordのID）
- 値: 対象リポジトリのパス（相対パスは `bughunter_bot` ディレクトリ基準）
- パスは絶対パスでも指定できます

フォーラムチャンネルIDは、Discordの開発者モードを有効化してチャンネルを右クリック → 「IDをコピー」で取得できます。

## 動作フロー
1. フォーラムに新規スレッド作成
2. Botがジョブを作成（オーナー投稿は自動開始、非オーナー投稿は承認待ち）
3. 対象リポジトリに `git pull` → worktree 作成
4. Claude Agent SDK で原因/仕様/修正を実施
5. コミット & PR作成
6. スレッドへ結果とPRリンクを投稿

## コマンド
- `/approve_job`: 承認待ち/失敗ジョブを開始（オーナーのみ）
- `/instruct_job`: 追加指示を送信して同じjobに再実行（オーナーのみ）
- `/rerun_job`: 完了ジョブをリセットして再実行（オーナーのみ）

## Discord Developer Portal で必要な設定
1. Bot タブ
   - **MESSAGE CONTENT INTENT** を有効化（スレッド本文取得のため）
2. OAuth2 → URL Generator
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Send Messages in Threads`, `Create Public Threads`, `Manage Threads`（必要に応じて）
3. サーバー側で、対象フォーラムチャンネルに Bot を追加し、上記権限が付与されていることを確認

## `/approve_job` が見えない/動かない場合
Discord 側に古いスラッシュコマンド定義が残っている可能性があります。以下を試してください。

1. `.env` に `DISCORD_GUILD_ID` を設定（ギルド同期にすると即時反映）
2. `.env` に `DISCORD_FORCE_COMMAND_SYNC=1` を設定
3. Bot を再起動

これで起動時に **既存コマンドをクリア → 再同期** します。

## 注意点
- オーナー投稿は自動実行、非オーナー投稿は `/approve_job` が必要です。
- Bot には `message_content` Intent が必要です（スレッド本文取得のため）。
- フォーラムチャンネル側の権限として `send_messages_in_threads` などが必要です。
- `repos/` と `worktrees/` はリポジトリ管理外です（.gitignore）。

## Z.AI を使いたい場合（Claude Code 公式外の経路）
Claude Code / Claude Agent SDK は公式には Anthropic API を前提としています。  
一方、Z.AI の公式ドキュメントでは「Anthropic 互換」として `ANTHROPIC_BASE_URL` と `ANTHROPIC_AUTH_TOKEN` を設定する方法が紹介されています。  
Claude Code の公式ドキュメントでは `ANTHROPIC_BASE_URL` が明記されていないため、動作は保証されません（Z.AI側の案内に従う形になります）。  

動作する場合は `.env` に以下を設定してください：

```
ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic
ANTHROPIC_AUTH_TOKEN=your_zai_api_key
ANTHROPIC_API_KEY=""
```
