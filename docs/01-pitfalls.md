# muxdesk — Pitfalls & Lessons（踩雷實錄）

把互動 Claude Code 包成 web 駕駛艙時，大多數複雜度來自「Claude Code 沒有給穩定的程式化介面」，只能 tail jsonl + 刮 tmux 畫面。以下是實際踩過的雷，含症狀 / 根因 / 修法，給日後維護或抽出開源者參考。

---

## A. 串流 / 即時預覽

### A1. jsonl 是「整段訊息才落檔」，沒有 token streaming
- **症狀**：送出後等很久才一次看到整段回覆。
- **根因**：Claude Code 把 assistant 訊息**完成才寫一行** jsonl（實測串流中行數不變、完成才跳）。所以靠 tail jsonl 的對話**天生沒有 streaming**。
- **修法**：另開 `/live`，串流期間 **capture-pane 刮 tmux pane** 抓進行中的回覆，落檔後抽換成乾淨 markdown。終端分頁本來就即時（直連 pty）。

### A2. 新版 claude TUI 串流時「沒有持續 spinner」
- **症狀**：用「畫面有 spinner ＝working」偵測，串流文字期間 working 一直 false。
- **根因**：spinner（`✲ Elucidating…`）只在**純思考期**短暫出現；文字一開始流就消失，回覆文字直接貼在輸入框上方、**與 idle 同版面**。
- **修法**：working 改用**內容變動偵測**（連續兩次 capture 的回覆塊不同）+ 純思考期 spinner 作輔助；回覆塊用「最底輸入框 prompt（❯）往上到第一個邊界」抽取，避免把舊對話 dump 進來（剪影）。

### A3. 即時預覽「一閃一閃」
- **症狀**：每次內容變動時整個預覽突然消失再出現。
- **根因**：① capture-pane 在 TUI 重繪/捲動的**瞬間偶爾抓到空白** → text 空 → working 掉 → 卸載。② show/hide 掛在 working（內容偵測）上，內容偵測一抖就閃。
- **修法（關鍵）**：**show/hide 不要用內容/working 偵測，改用「回合生命週期」**。`inTurn = 最後的(非中斷)user_message 索引 > 最後的結束標記索引`（結束標記＝`assistant_message`/`system_notice`/中斷訊息）。這個訊號在整段串流穩定不變、不依賴 flaky state、不靠空白偵測。

### A4. `assistant_thinking` 落在回合中間，害「末事件＝user_message」判斷失準
- **症狀**：claude 一思考，即時預覽就不觸發。
- **根因**：claude 思考會先落 `assistant_thinking` 事件 → 末事件不再是 user_message。
- **修法**：用**索引比較**而非「末事件是什麼」。`assistant_thinking` **不算結束標記**，所以思考＋串流整段 inTurn 仍為真。

---

## B. session 狀態機（最坑的一區）

### B1. readiness probe 每 4s 把 RUNNING_TOOL 蓋回 READY
- **症狀**：claude 在跑 tool，但 state 顯示 READY、停止鈕消失；或反過來，停止後一直卡 busy。
- **根因**：probe 用「畫面有 `accept edits on` footer」判 READY，但**那個 footer 跑 tool 時也一直在** → 每 4s force READY，蓋掉 transcript 驅動的互動態。
- **修法**：probe 設 READY 前排除「真正運行中」（`\(\d+s` live timer / `Running…` / `esc to interrupt`）。**注意**：別用裸 `✻`——completed 殘留「`✻ Verb for Ns`」也有 ✻，會讓 probe 永不設 READY → 卡 STARTING → 90s **startup timeout 變 ERROR**（這個我踩過）。

### B2. MANUAL_TAKEOVER 重播陷阱（每次重啟全卡）
- **症狀**：後端一重啟，**所有有歷史的 session 全變 MANUAL_TAKEOVER**，web 送訊息被拒。
- **根因**：rebind 用 `from_start` **重播整段 transcript**；而 writer 的「自己注入」記錄是 in-memory、重啟就清空 → 重播時連 web 注入過的歷史 user_message 都被當成「外部輸入」→ 觸發 MANUAL。
- **修法**：**移除「外部輸入→MANUAL_TAKEOVER」的自動偵測**（這個 mode 機制本來就要拔，「直接對話就好」）。

### B3. `submit_user_message` 閘序錯成死碼
- **症狀**：MANUAL 態的 session，web 送訊息永遠被拒、無法恢復。
- **根因**：先檢查 `can_accept_input()`（只認 READY）→ MANUAL 直接 return False，**下面的 auto-resume 永遠到不了**。
- **修法**：MANUAL/不可注入態**先 auto-resume 再過 ready 閘**。

### B4. interrupt 只送 Escape、不動 state
- **症狀**：按停止後，下面 state 沒偵測到、停止鈕不消失。
- **根因**：`interrupt()` 只 `send_key(Escape)`，而 Escape 取消 turn **不會發 turn-end 事件** → state 不會自己回 READY。
- **修法**：interrupt 後顯式 `on_interrupt()` 收斂 READY + 清待決 tool；前端 busy 別再用「末事件=user_message」當 fallback（中斷訊息本身就是 user_message）。

---

## C. 提問通道（muxdesk-ask）

### C1. 把 muxdesk-ask 做成「skill」反而製造 misbinding
- **症狀**：提問時對話噴出整份 SKILL.md（`Base directory for this skill: …`）+ claude 仍先試 `AskUserQuestion`（被 hook deny）再繞 Skill → 雙重雜訊。
- **根因**：① 命名成 skill → claude 會 invoke `Skill(muxdesk-ask)`，harness 把 SKILL.md 當 tool_result 灌進對話。② 一般 session 沒拿到 system prompt（只有 lead 有）→ claude 本能先用 AskUserQuestion。
- **修法**：**所有 session 注入 `BASE_SYSTEM_PROMPT`**：「提問**直接 Bash 跑 `scripts/muxdesk-ask` 絕對路徑**，別用 AskUserQuestion、**別先呼叫 muxdesk-ask skill**」。skill 只當說明文件（`--add-dir` 帶入讓他處 cwd 也發現）。前端再過濾掉殘留的 `AskUserQuestion`/`Skill(muxdesk-ask)` 工具事件（靠 tool_use_id 配對整對丟）。

### C2. PreToolUse hook 依賴工具命名
- **風險**：hook 比對 `tool_name == "AskUserQuestion"`，Anthropic 改名/改行為就失效。
- **緩解**：把它當「best-effort 導向」而非硬保證；muxdesk-ask 本身（Bash 工具）才是真通道。抽出時加版本防護 / 偵測失效時 fallback 提示。

### C3. AskUserQuestion 卡片的 Enter 會「打勾」而非送出
- **根因**：點選項後焦點留在那顆 `<button>`，按 Enter＝啟動該按鈕＝切換打勾。
- **修法**：卡片根節點 **capture 階段**攔 Enter → `preventDefault + stopPropagation` → 直接 advance（下一題/送出）。

---

## D. 雜項

- **D1. `/model` 有副作用**：claude 的 `/model X` 會「saved as your default for new sessions」→ 改到使用者**全域** `~/.claude/settings.json` 的 model。測試切換時誤改過，要小心還原（且 `.bak` 可能與現行分歧，別無腦整檔 cp）。
- **D2. 「Switch model?」確認框沒有 `Esc to cancel` footer** → 標準選單偵測（`_parse_menu`）抓不到；要獨立偵測 + 自動 Enter（游標預設在 Yes）。
- **D3. IME（注音）Enter**：組字中的 Enter 是「確認候選」不是送出 → 三層門禁（composing flag + `isComposing` + `keyCode===229` + compositionend 尾巴）。
- **D4. node 顯示 UUID**：Task subagent 沒有 name，後端只能回 agentId → 前端 label 改用 `description`（「Agent 1-A: …」）。
- **D5. 圖片服務防穿越**：`/image?path=` 必須限定在 `_IMG_DIR` 內（`resolve()` + startswith 檢查），否則任意讀檔。
- **D6. ruff/uvicorn cwd**：`cd frontend` 後 `$PWD` 變了，`tmux new-session -c "$PWD"` 會讓 uvicorn 從錯目錄啟動 import 失敗——重啟後端永遠用絕對路徑。
- **D7. 直接 `tmux send-keys` 注入會被當人類接手**（在 MANUAL 偵測移除前）→ 測試污染。移除偵測後才能安全用 tmux inject 測試。
