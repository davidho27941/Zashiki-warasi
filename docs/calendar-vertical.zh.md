# Calendar vertical(v1.4)

> English:[`calendar-vertical.md`](calendar-vertical.md).

當 email agent 把郵件分類為 `會議邀請` 或 `講座資訊` 時,v1.4 會路由
到 `calendar_sg` —— 一個 LangGraph subgraph,自動建立**tentative**
的 Google Calendar 事件,並在 Telegram 通知裡標出任何時段衝突。

**設計原則:絕不 auto-RSVP。** 所有事件建立時 `status: tentative`。
你在 Google Calendar 自己 UI 上確認或婉拒。`calendar_sg` **從不**
回送 RSVP 給邀請寄件者。

## 什麼會觸發

- Category: `會議邀請` 或 `講座資訊`(模組常數
  `_CALENDAR_LIKE_CATEGORIES`,在 `zashiki_warasi/agents/email_agent.py`)
- **`講座資訊` 邊界(v1.4.1 精修):** 必須**同時**具備具體活動日期/時間
  AND 特定地點或線上會議連結。課程推薦(Coursera、Udemy、MOOC 電子報)
  沒有明確 cohort 日期 → 分「廣告」,而非「講座資訊」。反例清單見
  analyze system prompt。
- **Extract short-circuit(v1.4.1 第二道防線):** 就算被誤分為
  `講座資訊`,extract 節點呼叫 LLM 前會先用輕量 regex 掃過
  `subject + body`。若**沒有任何日期/時間 pattern**(`YYYY-MM-DD`、
  `M/D`、`HH:MM`、中英星期等)→ 短路,回傳
  `CalendarSkipped(reason="no_event_signal")`,Telegram 顯示
  `📅 行事曆事件未建立: 內文無明確活動時間`,LLM 不會被呼叫。
  `.ics` 附件永遠繞過 short-circuit(結構化資料本身就是活動訊號)。
- Kill switch:`.env` 設 `CALENDAR_ENABLED=0` 完全停用 —— 行為與 v1.3 一致
- OAuth:需要 `https://www.googleapis.com/auth/calendar` scope(完整),和
  Gmail 共用同一個 credential(不是較窄的 `calendar.events` —— `freebusy.query`
  衝突偵測需要 full scope)

## 從 v1.3.x 升級

**一次性步驟:為新 scope 重新 auth。**

v1.4 image 預設 scope list 已擴充(Gmail readonly + Calendar events),
但 v1.3 的 OAuth token 沒包含新 scope。在你 reauth 之前,任何 Calendar
API 呼叫會回 403 —— `calendar_sg` 會 catch、在**每個 pod lifetime
只記一次** `calendar: OAuth scope not granted` WARNING,並優雅
降級為純文字通知。

授權新 scope:

```
curl -X POST http://<host>:8080/reauth -H "X-API-Key: $HTTP_API_KEY"
```

回應含 `auth_url`。用瀏覽器開,審視新的「查看與下載你的行事曆事件」授權,
按 Allow。Google redirect 回來後 `token.json` 就更新了新 scope。

搞定 —— 下一次 `POST /poll` 撞到 calendar-worthy category 就會用到
vertical。

## 你會看到什麼

**Telegram 通知**(除了標準摘要外):

```
📅 事件已建立 (tentative): 產品週會
   時間: 2026-09-15 14:00 – 15:00
   📍 信義區辦公室

⚠️ 該時段已有事件:
   • 14:00-14:30 週會
   • 14:00-14:30 1-on-1 with X

[ 🔗 View in Calendar ]      ← inline URL button
[ 📋 Copy trace ID ]         ← v1.3 button 保留
```

點 `🔗 View in Calendar` 打開對應事件的 Google Calendar UI 進行
確認 / 婉拒 / 編輯。點 `📋 Copy trace ID` 複製 trace_id
(v1.3 log→trace jump)。

**Google Calendar 條目:**
- Status: tentative(在日曆格上顯示為斜線紋)
- 標題、起訖時間、地點取自 extract 階段
- Description 內含衝突摘要 + 「來自郵件 X」的追蹤標記

## 抽取:`.ics` 優先、LLM fallback

現代邀請(Outlook、Google Calendar、Zoom)會夾 `text/calendar` 附件
(`.ics` 檔)。`calendar_sg` 用 `icalendar` Python 套件解析 —— 決定性、
符合 RFC 5545、100% 準確。

只有當郵件沒 `.ics` 時,vertical 才 fallback 到用 LLM 抽 body 內容。
LLM 被指示:任何欄位不確定就回 `null`;若 `{title, start, end}` 必填
組合不齊全,事件會被跳過,你會收到純文字 Telegram 通知標
`📅 行事曆事件未建立`。

## 衝突偵測

`events.insert` 前,`calendar_sg` 呼叫 Google Calendar 的
`freebusy.query` 查你 primary calendar。任何跟目標時段重疊的 busy
blocks 會 surface 到兩處:

- Telegram 通知(即時看到)
- 建立的事件的 description(未來去 Calendar UI review 時看得到)

**不論有沒有衝突,事件都會被建立為 tentative** —— 你可能想接受新邀請
且婉拒衝突那個,那個決定留給你。

## 設定

三個 env keys(全部 optional,預設適合 homelab 情境):

| Env var | 預設 | 用途 |
|---|---|---|
| `CALENDAR_ENABLED` | `1` | `0` 停用 vertical;calendar-worthy category 路由到 notify(v1.3 行為) |
| `CALENDAR_TIMEZONE` | `Asia/Taipei` | 套用到 floating time 事件(.ics 無 TZID,或 LLM 抽不出明確 tz) |
| `CALENDAR_PRIMARY_ID` | `primary` | 用於 freebusy 檢查 + 事件 insert 的 Google Calendar id。`primary` = 已認證帳號的 primary calendar |

## 冪等性

同封信被處理兩次(retry、checkpointer replay)不會建重複事件。
`calendar_sg` 算出 iCalUID:`.ics` 有 `UID` 就用,否則合成
`f"zashiki-{message_id}@zashiki-warasi.local"`。Google 對同 UID 的
第二次 insert 回 409 Conflict,vertical 當成 no-op success 處理。

對於**轉寄 / CC 過來、帶同一個真實 iCalUID** 的邀請,這樣就免費有跨郵件
去重。

## 用 Grafana 查

每次 `calendar_sg` 執行都會在 Tempo 產生一個 span tree:

```
POST /poll
└── zashiki.tick_once
    └── zashiki.node.calendar.extract
        └── zashiki.llm.chat            (只有沒 .ics 時才會有)
    └── zashiki.node.calendar.create
        (freebusy、events.list、events.insert 是 httpx auto-instrumentation
         的 span)
```

點 Telegram 通知的 `📋 Copy trace ID` → 貼到 Grafana → Tempo →
Search by Trace ID → 看完整流程 Gmail history 事件 → analyze → route
→ calendar extract + create → Telegram 送出。

## 停用

兩條路:

- **暫時停:** `.env` 設 `CALENDAR_ENABLED=0` + 重啟 pod。不建事件;
  calendar-worthy category 走 notify
- **永久停:** 到 Google 帳號 → 安全性 → 第三方 apps 撤銷
  calendar scope。Vertical catch 403 優雅降級
  (每個 pod lifetime 記一次 WARNING)

兩條路都不會動你行事曆已存在的 tentative 事件;如果要就到 Calendar UI
手動刪。

## Non-goals(延到未來 change 處理)

- Auto-RSVP(用 Telegram callback button 接受/婉拒)—— 需要 bot
  polling/webhook 基礎設施,我們沒有
- 循環事件超出首次 occurrence 的處理 —— RRULE 解析 + 跨 instance
  管理是更大的 scope
- Multi-calendar 衝突檢查 —— v1 只查 primary
- 出席者 auto-invite(以 organizer 身分)
- Invitation revision 更新 —— 需要跨郵件狀態

## 相關

- OpenSpec change:`openspec/changes/archive/YYYY-MM-DD-add-calendar-vertical/`
  (proposal + design + spec deltas)
- v1.3 log→trace jump:[`observability.zh.md`](observability.zh.md)
  「Telegram Copy trace ID 按鈕」
- Expense vertical(平行 pattern):
  `src/zashiki_warasi/agents/verticals/expense.py`
