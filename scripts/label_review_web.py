"""Local web UI for blind relabeling — companion to label_review.py.

Same input/output contract as `label_review.py --blind`, but in a
browser instead of a terminal. Serves on 127.0.0.1 only, loads no
external resources (the page works fully offline), and never sends a
model label to the browser — blindness is enforced server-side.

    uv run python scripts/label_review_web.py \
        --input data/relabel/phase2_candidates.jsonl \
        --output data/relabel/phase2_verified.jsonl
    # then open http://127.0.0.1:8765

Verdicts append to --output in the CLI's record format, so the CLI and
the web UI can share one output file and resume each other. "Undo"
re-labels by appending a newer record for the same message_id —
consumers must take the LAST record per message_id.

PRIVACY: input and output live in gitignored data/ — real mail text.
"""

from __future__ import annotations

import argparse
import errno
import json
import random
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from label_review import CATEGORIES, HOTKEYS

PASSTHROUGH = ("laya_category", "laya_confidence", "bucket")
FLAGS = ("求職", "不確定")


def _load_done(path: str) -> set[str]:
    done: set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    done.add(json.loads(line)["message_id"])
    except FileNotFoundError:
        pass
    return done


def make_handler(rows: dict[str, dict], order: list[str], output: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def do_GET(self):
            if self.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                done = _load_done(output)
                pending = [
                    # Blind: only the text the human needs — no labels.
                    {"message_id": m,
                     "input_text": rows[m]["input_text"],
                     "analyzed_at": rows[m].get("analyzed_at", "")[:16]}
                    for m in order if m not in done
                ]
                self._json(200, {
                    "total": len(order), "done": len(order) - len(pending),
                    "rows": pending,
                    "categories": [{"key": k, "name": c}
                                   for k, c in zip(HOTKEYS, CATEGORIES)],
                    "flags": list(FLAGS),
                })
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/verdict":
                self._send(404, b"not found", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                row = rows[req["message_id"]]
                verified = req["verified"]
                if verified not in CATEGORIES:
                    raise ValueError(f"unknown category {verified!r}")
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return

            flags = [f for f in req.get("flags", []) if f in FLAGS]
            record = {
                "message_id": row["message_id"],
                "llm_original": row["category"],
                "verified": verified,
                "corrected": verified != row["category"],
                "verified_at": datetime.now(tz=timezone.utc).isoformat(),
                "blind": True,
            }
            for key in PASSTHROUGH:
                if key in row:
                    record[key] = row[key]
            if flags:
                record["flags"] = flags
            note = "; ".join(flags + [n for n in [req.get("note", "").strip()] if n])
            if note:
                record["note"] = note
            with open(output, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._json(200, {"ok": True})

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="data/relabel/phase2_candidates.jsonl")
    ap.add_argument("--output", default="data/relabel/phase2_verified.jsonl")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rows: dict[str, dict] = {}
    with open(args.input, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                rows[row["message_id"]] = row
    order = list(rows)
    random.Random(args.seed).shuffle(order)

    try:
        server = HTTPServer(("127.0.0.1", args.port),
                            make_handler(rows, order, args.output))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(f"port {args.port} is already in use — another "
              f"label_review_web may already be running (just open "
              f"http://127.0.0.1:{args.port}), or pass --port N. "
              f"Two instances on one --output would interleave writes.",
              file=sys.stderr)
        return 1
    done = len(_load_done(args.output) & set(rows))
    print(f"{len(rows)} rows ({done} already labeled) → "
          f"http://127.0.0.1:{args.port}   (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


PAGE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Relabel 標註</title>
<style>
  :root {
    --bg: #f6f5f1; --panel: #ffffff; --text: #23211d; --muted: #6f6b63;
    --line: #e2dfd6; --accent: #2f6f5e; --accent-text: #ffffff;
    --chip: #efece4; --warn: #9a5b13;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1b1d1f; --panel: #242729; --text: #e4e1da; --muted: #9a968d;
      --line: #34383b; --accent: #6fb39e; --accent-text: #10201b;
      --chip: #2e3235; --warn: #e0a85a;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 17px/1.75 -apple-system, "PingFang TC", "Noto Sans TC",
          "Hiragino Sans", "Segoe UI", sans-serif;
  }
  main { max-width: 860px; margin: 0 auto; padding: 24px 16px 48px; }
  header { display: flex; align-items: center; gap: 16px; margin-bottom: 16px; }
  header h1 { font-size: 18px; margin: 0; font-weight: 600; }
  .progress { flex: 1; height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; }
  .progress > div { height: 100%; background: var(--accent); transition: width .2s; }
  .count { color: var(--muted); font-size: 14px; font-variant-numeric: tabular-nums; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 20px 22px; }
  .meta { color: var(--muted); font-size: 13px; margin-bottom: 10px; font-family: ui-monospace, monospace; }
  .mail {
    white-space: pre-wrap; word-break: break-word; margin: 0;
    max-height: 46vh; overflow-y: auto; padding-right: 6px;
    font: inherit;
  }
  .mail .subject { font-weight: 650; display: block; margin-bottom: 8px; }
  h2 { font-size: 13px; color: var(--muted); font-weight: 600; margin: 20px 0 8px; letter-spacing: .04em; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }
  .opt { position: relative; }
  .opt input { position: absolute; opacity: 0; pointer-events: none; }
  .opt span {
    display: flex; align-items: center; gap: 8px; padding: 9px 12px;
    border: 1px solid var(--line); border-radius: 8px; cursor: pointer;
    background: var(--panel); user-select: none;
  }
  .opt span kbd {
    font: 12px ui-monospace, monospace; background: var(--chip); color: var(--muted);
    border-radius: 4px; padding: 1px 6px; min-width: 20px; text-align: center;
  }
  .opt input:checked + span { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
  .opt input:checked + span kbd { background: rgba(0,0,0,.15); color: inherit; }
  .opt input:focus-visible + span { outline: 2px solid var(--accent); outline-offset: 2px; }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .note { flex: 1; min-width: 200px; padding: 9px 12px; border-radius: 8px;
          border: 1px solid var(--line); background: var(--panel); color: var(--text); font: inherit; }
  .actions { display: flex; gap: 8px; margin-top: 20px; }
  button {
    font: inherit; padding: 9px 18px; border-radius: 8px; cursor: pointer;
    border: 1px solid var(--line); background: var(--panel); color: var(--text);
  }
  button.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); margin-left: auto; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .hint { color: var(--muted); font-size: 13px; margin-top: 12px; }
  .err { color: var(--warn); font-size: 14px; margin-top: 8px; min-height: 1em; }
  .done { text-align: center; padding: 64px 0; }
  @media (max-width: 520px) {
    .grid { grid-template-columns: repeat(2, 1fr); }
    .actions { flex-wrap: wrap; }
    button.primary { margin-left: 0; width: 100%; }
  }
</style>
</head>
<body>
<main>
  <header>
    <h1>Relabel(盲標)</h1>
    <div class="progress"><div id="bar" style="width:0"></div></div>
    <div class="count" id="count">…</div>
  </header>
  <div id="app"></div>
</main>
<script>
let S = { cats: [], flags: [], queue: [], total: 0, done: 0, history: [], prefill: null };
const $ = (s) => document.querySelector(s);
const esc = (t) => t.replace(/[&<>]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

async function load() {
  const r = await fetch("/api/state");
  const d = await r.json();
  Object.assign(S, { cats: d.categories, flags: d.flags, queue: d.rows, total: d.total, done: d.done });
  render();
}

function progress() {
  $("#count").textContent = `${S.done} / ${S.total}`;
  $("#bar").style.width = `${S.total ? (S.done / S.total) * 100 : 0}%`;
}

function render() {
  progress();
  const row = S.queue[0];
  if (!row) {
    $("#app").innerHTML = `<div class="card done"><h2>全部完成</h2>
      <p>共 ${S.done} 筆已標註,結果已寫入輸出檔。可以關掉這個分頁與終端機的 server 了。</p>
      ${S.history.length ? '<button id="undo">← 修改上一筆</button>' : ''}</div>`;
    if (S.history.length) $("#undo").onclick = undo;
    return;
  }
  const nl = row.input_text.indexOf("\n");
  const subject = nl === -1 ? row.input_text : row.input_text.slice(0, nl);
  const body = nl === -1 ? "" : row.input_text.slice(nl + 1).replace(/^\n+/, "");
  const pre = S.prefill || {};
  $("#app").innerHTML = `
    <div class="card">
      <div class="meta">${esc(row.message_id)} · ${esc(row.analyzed_at)}</div>
      <pre class="mail"><span class="subject">${esc(subject)}</span>${esc(body)}</pre>
    </div>
    <h2>類別(單選)</h2>
    <div class="grid">${S.cats.map((c) => `
      <label class="opt"><input type="radio" name="cat" value="${c.name}" ${pre.verified === c.name ? "checked" : ""}>
        <span><kbd>${c.key}</kbd>${c.name}</span></label>`).join("")}
    </div>
    <h2>標記(可複選)</h2>
    <div class="row">${S.flags.map((f) => `
      <label class="opt"><input type="checkbox" name="flag" value="${f}" ${(pre.flags || []).includes(f) ? "checked" : ""}>
        <span>${f}</span></label>`).join("")}
      <input class="note" id="note" placeholder="備註(選填)" value="${esc(pre.note || "")}">
    </div>
    <div class="err" id="err"></div>
    <div class="actions">
      <button id="undo" ${S.history.length ? "" : "disabled"}>← 上一筆</button>
      <button id="skip">跳過</button>
      <button id="submit" class="primary">送出 ⏎</button>
    </div>
    <div class="hint">鍵盤:1–9 / 0 / a–f 選類別 · Enter 送出 · s 跳過 · ← 上一筆(在備註欄打字時快捷鍵暫停)</div>`;
  S.prefill = null;
  $("#undo").onclick = undo;
  $("#skip").onclick = skip;
  $("#submit").onclick = submit;
}

async function submit() {
  const row = S.queue[0];
  const cat = document.querySelector('input[name="cat"]:checked');
  if (!cat) { $("#err").textContent = "請先選一個類別"; return; }
  const payload = {
    message_id: row.message_id, verified: cat.value,
    flags: [...document.querySelectorAll('input[name="flag"]:checked')].map((x) => x.value),
    note: $("#note").value,
  };
  $("#submit").disabled = true;
  try {
    const r = await fetch("/api/verdict", { method: "POST", body: JSON.stringify(payload) });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || "寫入失敗");
  } catch (e) {
    $("#err").textContent = "寫入失敗:" + e.message + "(server 還在跑嗎?)";
    $("#submit").disabled = false;
    return;
  }
  S.history.push({ row: S.queue.shift(), verdict: payload });
  S.done += 1;
  render();
}

function skip() {
  if (S.queue.length > 1) S.queue.push(S.queue.shift());
  render();
}

function undo() {
  const last = S.history.pop();
  if (!last) return;
  S.queue.unshift(last.row);
  S.done -= 1;  // re-submitting appends a newer record; last one wins
  S.prefill = last.verdict;
  render();
}

document.addEventListener("keydown", (e) => {
  if (e.target.id === "note") {
    if (e.key === "Enter") { e.preventDefault(); submit(); }
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey || !S.queue.length) return;
  const k = e.key.toLowerCase();
  const cat = S.cats.find((c) => c.key === k);
  if (cat) {
    const el = document.querySelector(`input[name="cat"][value="${cat.name}"]`);
    if (el) { el.checked = true; $("#err").textContent = ""; }
  } else if (e.key === "Enter") { e.preventDefault(); submit(); }
  else if (k === "s") skip();
  else if (e.key === "ArrowLeft") undo();
});

load();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
