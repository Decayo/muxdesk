# cc-desk — Packaging / Extraction（抽出開源清單）

把 cc-desk 從 ibkr-trade-journal 抽成獨立 package 時要做的事。目標：一個 `pip install cc-desk` + `npx`/靜態前端就能跑的「web cockpit for interactive Claude Code in tmux」。

## 1) 要搬走的東西

| 類別 | 檔案 / 目錄 |
|---|---|
| 後端 app | `cc_desk_standalone.py`（FastAPI standalone） |
| 後端服務 | `ibkr_show_backend/app/services/cc_desk/`（session_manager / state_machine / transcript_parser / tmux_driver / event_bus / session_registry / artifact_detector / team/） |
| 提問通道 | `scripts/cc-ask`、`scripts/cc-ask-hook` |
| 前端 | `frontend/src/pages/CcDesk*.tsx`、`frontend/src/components/ccDesk/*`、`frontend/src/api/nativeAgents.ts`、`frontend/src/api/http.ts`、`frontend/src/stores/{session,transcript}Store.ts`、`frontend/src/hooks/useCcDeskStream.ts`、`frontend/src/types/ccDesk.ts` |
| 啟動 | `cc-desk-serve.sh` |
| skill | `.claude/skills/cc-ask/SKILL.md` |
| 文件 | `docs/cc-desk/`（本目錄） |

## 2) 要解的耦合（跟 ibkr-trade-journal 綁的）

- **路徑命名空間**：後端路由 `PREFIX = /api/agent/cc-desk`、`app.services.cc_desk` import 路徑——抽出後改成獨立模組（如 `cc_desk/`），standalone 直接 import，不再依賴 `ibkr_show_backend`。
- **HARNESS bar**：`CcHarnessBar`（進場/復盤/Gate/Inbox… 那排）是 trade-journal 專屬的 Plan-Gated 交易快捷鍵 → 抽出時拿掉或做成可配置的「快捷指令」插槽。
- **harness-stock-vault / IBKR**：cc-desk 本體不依賴交易邏輯；只是預設 workspace 指向那；改成 config。
- **前端外殼**：`IBKR Trade Desk` tab、PrimeVue 風格 token（`surface-panel`/CSS variable）——抽出時換成中性主題。

## 3) 執行期依賴（要在 README 寫清楚）

- **必須**：`tmux`、`claude`（Claude Code CLI，登入好）、Python 3.11+、FastAPI/uvicorn。
- **路徑假設**：`~/.claude/projects/<cwd-slug>/<uuid>.jsonl`（transcript）、`~/.claude/teams`、`<sid>/subagents/*.meta.json`、`~/.claude/settings.json`（hook 注入）——這些是 **Claude Code 內部慣例，非公開 API**，版本升級可能變（見 [01-pitfalls.md](./01-pitfalls.md) C2）。
- **前端**：React 19 + Vite + `@xyflow/react`(React-Flow) + `react-markdown` + `remark-gfm` + `mermaid` + `dagre`。
- **無**新外部服務（不需 DB；session registry 是檔案 + in-memory）。

## 4) 抽出時要硬化的點（PAL/Perplexity 也點名）

1. **capture-pane 解析脆弱**：對終端 resize / ANSI escape 變動敏感。
   - 固定 pane 寬高（已有 PtyBridge cols/rows）；解析加 error boundary；`/live` 抽取失敗就回空、不要噴亂碼。
   - 把 spinner / 邊界 / 狀態列的 regex 集中成「TUI sentinel 表」，方便隨 claude 版本調。
2. **PreToolUse hook 依賴 `AskUserQuestion` 命名**：Anthropic 改名即失效。
   - 當 best-effort；cc-ask（Bash 工具）才是真通道。偵測 hook 沒生效時退回提示。
3. **transcript schema 依賴**：`parse_record` 綁 jsonl 欄位（`toolUseID`/`content[].type` 等）。
   - 集中在 `transcript_parser.py`，升級時只改一處；加未知事件的容錯（skip 不 crash）。
4. **state 收斂規則**：互動 state 由 transcript 推、啟動/阻塞由 probe 推、互動 busy 不被 probe 蓋——這套規則要在 `state_machine.py` 註解清楚（見 01-pitfalls B 區）。
5. **可配置化**：port（8001/5274）、`claude` 命令、permission-mode、workspace、`/tmp/cc-desk-*` 目錄 → 環境變數 / config。

## 5) 建議的 package 形狀

```
cc-desk/
  pyproject.toml            # pip install cc-desk → console_script `cc-desk`
  cc_desk/
    app.py                  # = cc_desk_standalone.py（FastAPI）
    session/                # manager/state_machine/transcript_parser/tmux_driver/...
    ask/                    # cc-ask, cc-ask-hook（裝成 data files / 自帶絕對路徑）
    static/                 # build 好的前端（vite build → 後端 serve）
  frontend/                 # 原始前端（dev 用），build 進 static/
  docs/                     # 本目錄
  README.md                 # 執行期依賴 + 快速開始 + 已知脆弱點
```

- `cc-desk serve` 一鍵起後端 + serve 靜態前端（單 port），免 tmux serve script。
- cc-ask / cc-ask-hook 路徑：package 內自帶，啟動時算絕對路徑注入（現在已是這做法）。

## 6) 命名 / 授權

- 取個中性名（cc-desk / claude-cockpit / ttmux-claude…）。
- 註明依賴 Claude Code（Anthropic 商品）+ 用到其 **未公開的 jsonl/hook 慣例**，可能隨版本失效——這是這類工具的共同風險（observer / octomux 也一樣）。
