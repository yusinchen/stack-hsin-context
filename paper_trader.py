#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stack_FBS 模擬投資人 — GitHub Actions 版
流程：fetch paper-feed + TWSE/TPEx 收盤價 → Claude API 做裁量決策（回 JSON）
     → 程式驗算並套用（手續費/稅/鐵律）→ 更新 ledger.json / JOURNAL.md → LINE 推播（選配）
金額計算一律由程式執行，Claude 只負責「決策與理由」。不構成投資建議。
"""
import json
import math
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
NOW = datetime.now(TZ)
TODAY = NOW.date()

FEED_URL = os.environ.get("FEED_URL", "https://hsin-trading-stackapp.fly.dev/api/public/paper-feed")
STATE_MD_URL = "https://raw.githubusercontent.com/yusinchen/stack-hsin-context/main/project-state.md"
MODEL = os.environ.get("PAPER_TRADER_MODEL") or "claude-sonnet-5"
LEDGER_PATH = "paper_trading/ledger.json"
JOURNAL_PATH = "paper_trading/JOURNAL.md"

FEE_RATE, FEE_MIN, TAX_RATE = 0.001425, 20, 0.003


def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


def http_get(url, timeout=30, retries=3, headers=None):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": "stack-fbs-paper-trader/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  fetch 失敗 ({i+1}/{retries}) {url.split('?')[0]}: {e}")
            time.sleep(3 * (i + 1))
    log(f"  放棄：{last}")
    return None


# ---------- 資料抓取 ----------

def fetch_feed():
    log("抓取 paper-feed …")
    raw = http_get(FEED_URL)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log("  feed 非合法 JSON")
        return None


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return None


def fetch_twse_closes():
    """回傳 (closes: {code: close}, is_today: bool)。主來源 MI_INDEX，備援 STOCK_DAY_ALL。"""
    log("抓取 TWSE 收盤 (MI_INDEX) …")
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={TODAY:%Y%m%d}&type=ALLBUT0999&response=json"
    raw = http_get(url)
    closes = {}
    if raw:
        try:
            data = json.loads(raw)
            for tbl in data.get("tables", []):
                fields = tbl.get("fields") or []
                if "證券代號" in fields and "收盤價" in fields:
                    i_code, i_close = fields.index("證券代號"), fields.index("收盤價")
                    for row in tbl.get("data", []):
                        c = _num(row[i_close])
                        if c is not None:
                            closes[str(row[i_code]).strip()] = c
        except Exception as e:  # noqa: BLE001
            log(f"  MI_INDEX 解析失敗: {e}")
    if closes:
        log(f"  TWSE 當日收盤 {len(closes)} 檔")
        return closes, True
    log("  MI_INDEX 無資料（休市或未產出），改試 STOCK_DAY_ALL 備援…")
    raw = http_get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    if raw:
        try:
            rows = json.loads(raw)
            d0 = rows[0].get("Date", "") if rows else ""
            is_today = d0 == f"{TODAY:%Y%m%d}"
            for r in rows:
                c = _num(r.get("ClosingPrice"))
                if c is not None:
                    closes[str(r.get("Code", "")).strip()] = c
            log(f"  備援 {len(closes)} 檔，資料日 {d0}（{'今日' if is_today else '非今日'}）")
            return closes, is_today
        except Exception as e:  # noqa: BLE001
            log(f"  備援解析失敗: {e}")
    return {}, False


def fetch_tpex_closes():
    log("抓取 TPEx 收盤 …")
    raw = http_get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
    closes, is_today = {}, False
    if raw:
        try:
            rows = json.loads(raw)
            roc_today = f"{TODAY.year - 1911}{TODAY:%m%d}"
            d0 = str(rows[0].get("Date", "")) if rows else ""
            is_today = d0 == roc_today
            for r in rows:
                c = _num(r.get("Close"))
                if c is not None:
                    closes[str(r.get("SecuritiesCompanyCode", "")).strip()] = c
            log(f"  TPEx {len(closes)} 檔，資料日 {d0}（{'今日' if is_today else '非今日'}）")
        except Exception as e:  # noqa: BLE001
            log(f"  TPEx 解析失敗: {e}")
    return closes, is_today


# ---------- 費用計算 ----------

def buy_fee(amount):
    return max(FEE_MIN, round(amount * FEE_RATE))


def sell_costs(mv):
    return max(FEE_MIN, round(mv * FEE_RATE)), round(mv * TAX_RATE)


# ---------- 主邏輯 ----------

def parse_dt(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s)[:19], fmt).date()
        except ValueError:
            continue
    return None


def dedupe_watchpool(items):
    seen, out = set(), []
    for it in items:
        key = (it.get("symbol"), it.get("trigger_price"))
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def get_close(sym, twse, tpex):
    return twse.get(sym) if sym in twse else tpex.get(sym)


def review_positions(positions, twse, tpex):
    out = []
    for p in positions:
        close = get_close(p["symbol"], twse, tpex)
        r = {"symbol": p["symbol"], "name": p.get("name"), "close": close}
        if close:
            fee, tax = sell_costs(close * p["shares"])
            net = close * p["shares"] - fee - tax - p["total_cost"]
            r.update({
                "vs_entry_pct": round((close / p["entry_price"] - 1) * 100, 2),
                "stop_loss": p.get("stop_loss"),
                "stop_hit": bool(p.get("stop_loss")) and close <= p["stop_loss"],
                "take_profit_low": p.get("take_profit_low"),
                "tp_hit": bool(p.get("take_profit_low")) and close >= p["take_profit_low"],
                "unrealized_net_twd": round(net, 1),
            })
        exp = parse_dt(p.get("expiry_date"))
        if exp:
            r["expiry_date"] = str(exp)
            r["expired"] = TODAY >= exp
        out.append(r)
    return out


def rules_decision(reviews):
    """純規則守門（零 API 費）：只管出場風控，不做進場裁量。"""
    holdings = []
    for r in reviews:
        if r.get("expired"):
            act, why = "exit", "10 交易日熔斷到期，規則出場"
        elif r.get("stop_hit"):
            act, why = "exit", f"收盤 {r['close']} 跌破停損 {r['stop_loss']}，規則出場"
        elif r.get("tp_hit"):
            act, why = "exit", f"收盤 {r['close']} 達停利下緣 {r['take_profit_low']}，規則出場"
        elif r.get("close") is None:
            act, why = "hold", "無今日收盤價，規則續抱（下輪補檢）"
        else:
            act, why = "hold", (f"收盤 {r['close']} 未觸及停損 {r.get('stop_loss')}／"
                                f"停利 {r.get('take_profit_low')}，規則續抱")
        holdings.append({"symbol": r["symbol"], "action": act, "reason_md": why})
    return {"gate_text": "（規則守門模式，不判 gate）", "holdings": holdings, "entries": [],
            "no_entry_reason_md": "規則守門模式只做出場風控；進場裁量由手機版 Claude 專案執行。",
            "line_summary": "規則守門：停損/停利/到期檢查完成。"}


def call_claude(prompt):
    import anthropic
    log(f"呼叫 Claude API（{MODEL}）…")
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=3000,
        temperature=0.3,
        system=(
            "你是台股波段紙上交易的裁量決策引擎，輸出僅限一個 JSON 物件，不要有任何其他文字。"
            "所有金額計算由外部程式執行，你只負責決策與理由。理由用繁體中文＋台股術語，紅=多/買、綠=空/賣。"
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"Claude 回覆無 JSON：{text[:500]}")
    log(f"  用量：in={msg.usage.input_tokens} out={msg.usage.output_tokens}")
    return json.loads(m.group(0))


def build_prompt(mode, ledger, reviews, wp_items, gate, scan_results, state_md, journal_tail):
    capital = ledger["meta"]["capital_twd"]
    rules = "\n".join(f"- {r}" for r in ledger["meta"]["rules"])
    parts = [
        f"今天是 {TODAY}（台灣時間，盤後）。模式：{mode}。",
        "",
        "== 方法論摘要（節錄）==",
        (state_md or "（取得失敗，依下方帳本規則）")[:3500],
        "",
        f"== 帳本規則（本金 {capital:,} 元）==", rules,
        f"現金：{ledger['cash']:.2f}；暫停新倉至：{ledger['state'].get('paused_until') or '無'}；"
        f"連續虧損筆數：{ledger['stats'].get('consecutive_losses', 0)}",
        "",
        "== 持倉與今日收盤檢視（程式已算好，數字勿改）==",
        json.dumps({"positions": ledger["positions"], "reviews": reviews}, ensure_ascii=False),
        "",
    ]
    if mode == "full":
        parts += [
            "== 大盤 gate（系統輸出）==", json.dumps(gate, ensure_ascii=False),
            "",
            "== B1 準備池（已去重）==", json.dumps(wp_items, ensure_ascii=False),
            "",
            "== 系統A 推薦（節錄前 40 檔）==",
            json.dumps(scan_results[:40], ensure_ascii=False),
            "",
            "進場規則提醒：B1 只跟 status=已觸發 且觸發/更新日為今日或前一交易日者，進場價=trigger_price（註記 T+1 偏差）；"
            "A 需三關卡＋看得懂、寧缺勿濫；不事後追高。空手不進場是合法決策但必須寫理由。"
            "部位規模不設上限：好機會可以全押（現金花完也沒關係），唯單筆 ≥7,000 元。",
        ]
    else:
        parts += ["== 注意 ==", "本輪僅做持倉風控（feed 無今日資料），entries 必須為空陣列，並在 no_entry_reason_md 說明。"]
    parts += [
        "",
        "== 近期日誌（尾段節錄）==", journal_tail[-2500:],
        "",
        "請輸出 JSON（僅此物件）：",
        '''{
 "gate_text": "大盤 gate 判定一句話",
 "holdings": [{"symbol": "4763", "action": "hold|exit", "reason_md": "續抱/出場理由（含收盤 vs 停損/停利/劇本）"}],
 "entries": [{"symbol": "", "name": "", "market": "TWSE|TPEX", "system": "A|B1", "entry_price": 0.0,
              "amount_twd": 0, "stop_loss": 0.0, "take_profit_low": 0.0, "take_profit_high": 0.0,
              "expiry_date": "YYYY-MM-DD", "note": "劇本備註", "reason_md": "進場理由"}],
 "no_entry_reason_md": "若 entries 為空，說明為什麼不進場（重點欄位）",
 "line_summary": "給 LINE 的 2-3 行摘要（不含帳本數字，程式會補）"
}''',
        "",
        "硬性規則：reviews 中 expired=true 的持倉 action 必須為 exit；stop_hit=true 應出場（除非有極強理由並說明）；"
        "entries 的 symbol 必須來自上方準備池（B1）或系統A推薦（A）名單。",
    ]
    return "\n".join(parts)


def apply_decisions(decision, ledger, reviews, wp_items, scan_results, twse, tpex, mode):
    """回傳 (exit_lines, entry_lines, warnings)；直接修改 ledger。"""
    warnings, exit_lines, entry_lines = [], [], []
    rev_by_sym = {r["symbol"]: r for r in reviews}

    # --- 出場（含強制熔斷）---
    actions = {h.get("symbol"): h for h in decision.get("holdings", [])}
    for p in list(ledger["positions"]):
        sym = p["symbol"]
        r = rev_by_sym.get(sym, {})
        act = (actions.get(sym) or {}).get("action", "hold")
        reason = (actions.get(sym) or {}).get("reason_md", "")
        if r.get("expired") and act != "exit":
            act, reason = "exit", (reason + "（10 交易日熔斷到期，程式強制出場）").strip()
            warnings.append(f"{sym} 到期未出場，程式強制出場")
        if act != "exit":
            continue
        close = r.get("close")
        if not close:
            warnings.append(f"{sym} 無收盤價，無法出場，改續抱")
            continue
        mv = close * p["shares"]
        fee, tax = sell_costs(mv)
        net = round(mv - fee - tax - p["total_cost"], 1)
        ledger["cash"] = round(ledger["cash"] + mv - fee - tax, 2)
        ledger["positions"].remove(p)
        ledger["closed_trades"].append({**p, "exit_date": str(TODAY), "exit_price": close,
                                        "sell_fee": fee, "sell_tax": tax, "net_pnl_twd": net,
                                        "exit_reason": reason})
        st = ledger["stats"]
        st["total_closed"] += 1
        st["realized_pnl_net_twd"] = round(st.get("realized_pnl_net_twd", 0) + net, 1)
        if net < 0:
            st["losses"] += 1
            st["consecutive_losses"] = st.get("consecutive_losses", 0) + 1
        else:
            st["wins"] += 1
            st["consecutive_losses"] = 0
        color = "🔴" if net >= 0 else "🟢"
        exit_lines.append(f"{color} **出場 {sym} {p.get('name','')}**：{p['shares']} 股 @{close}，"
                          f"淨損益 {net:+,.1f}（費 {fee}＋稅 {tax}）。{reason}")

    if ledger["stats"].get("consecutive_losses", 0) >= 2:
        ledger["state"]["paused_until"] = str(TODAY + timedelta(days=7))
        warnings.append(f"連續虧損 {ledger['stats']['consecutive_losses']} 筆 → 暫停新倉至 {ledger['state']['paused_until']}")

    # --- 進場 ---
    paused = ledger["state"].get("paused_until")
    paused_active = bool(paused) and TODAY <= (parse_dt(paused) or TODAY - timedelta(days=1))
    wp_syms = {it.get("symbol") for it in wp_items}
    scan_syms = {r.get("symbol") for r in scan_results}
    for e in decision.get("entries", []):
        sym = str(e.get("symbol", "")).strip()
        if mode != "full":
            warnings.append(f"{sym} 風控模式不開新倉，忽略")
            continue
        if paused_active:
            warnings.append(f"{sym} 處於暫停新倉期，忽略")
            continue
        if e.get("system") == "B1" and sym not in wp_syms:
            warnings.append(f"{sym} 不在準備池名單，拒絕")
            continue
        if e.get("system") == "A" and sym not in scan_syms:
            warnings.append(f"{sym} 不在系統A推薦名單，拒絕")
            continue
        price = _num(e.get("entry_price")) or get_close(sym, twse, tpex)
        if not price:
            warnings.append(f"{sym} 無進場價，拒絕")
            continue
        amount = _num(e.get("amount_twd")) or 0
        shares = math.floor(amount / price)
        if shares * price < 7000:
            shares = math.ceil(7000 / price)
        cost = shares * price
        fee = buy_fee(cost)
        while shares > 0 and cost + fee > ledger["cash"]:
            shares -= 1
            cost = shares * price
            fee = buy_fee(cost)
        if shares <= 0 or cost < 7000:
            warnings.append(f"{sym} 資金不足或不符 ≥7000 鐵律（cash={ledger['cash']:.0f}），拒絕")
            continue
        ledger["cash"] = round(ledger["cash"] - cost - fee, 2)
        ledger["positions"].append({
            "symbol": sym, "name": e.get("name", ""), "market": e.get("market", "TWSE"),
            "system": e.get("system", "B1"), "entry_date": str(TODAY),
            "entry_price": price, "shares": shares, "amount": round(cost, 2),
            "buy_fee": fee, "total_cost": round(cost + fee, 2),
            "stop_loss": _num(e.get("stop_loss")), "take_profit_low": _num(e.get("take_profit_low")),
            "take_profit_high": _num(e.get("take_profit_high")), "expiry_date": e.get("expiry_date"),
            "note": e.get("note", ""),
        })
        entry_lines.append(f"🔴 **進場 {sym} {e.get('name','')}（{e.get('system')}）**：{shares} 股 @{price}"
                           f"＝{cost:,.1f}，買費 {fee} → 成本 {cost+fee:,.1f}。"
                           f"停損 {e.get('stop_loss')}／停利 {e.get('take_profit_low')}–{e.get('take_profit_high')}"
                           f"／到期 {e.get('expiry_date')}。{e.get('reason_md','')}")
    return exit_lines, entry_lines, warnings


def render_journal(mode, decision, ledger, reviews, exit_lines, entry_lines, warnings, twse, tpex):
    mv_total = 0.0
    hold_lines = []
    rev_by_sym = {r["symbol"]: r for r in reviews}
    for p in ledger["positions"]:
        r = rev_by_sym.get(p["symbol"], {})
        close = r.get("close")
        mv = (close or p["entry_price"]) * p["shares"]
        mv_total += mv
        if p.get("entry_date") == str(TODAY):
            continue  # 當日新進場，已列在進場決策
        h = next((x for x in decision.get("holdings", []) if x.get("symbol") == p["symbol"]), {})
        num = (f"收 {close}（vs 進場 {r.get('vs_entry_pct'):+}%）、未實現 {r.get('unrealized_net_twd'):+,.1f}"
               if close else "無今日收盤價")
        hold_lines.append(f"  - 🔴 **{p['symbol']} {p.get('name','')} 續抱**：{num}。{h.get('reason_md','')}")
    if not ledger["positions"] and not exit_lines:
        hold_lines.append("  - 空手，無持倉議題。")

    mode_label = {"full": "", "risk_only": "（feed 過期，僅持倉風控）",
                  "rules_only": "（規則守門，零 API）"}.get(mode, "")
    lines = [f"### {TODAY:%Y/%m/%d} {NOW:%H:%M}（盤後）— GitHub Actions 自動跑" + mode_label,
             "", f"- 大盤 gate：{decision.get('gate_text', 'n/a')}",
             "- 持倉檢視：", *hold_lines,
             "- 進場決策：" + ("" if entry_lines else decision.get("no_entry_reason_md", "不進場。"))]
    lines += [f"  - {l}" for l in entry_lines]
    lines.append("- 出場決策：" + ("無。" if not exit_lines else ""))
    lines += [f"  - {l}" for l in exit_lines]
    if warnings:
        lines.append("- ⚠️ 程式警示：" + "；".join(warnings))
    st = ledger["stats"]
    lines.append(f"- 帳本：現金 {ledger['cash']:,.2f} / 持倉市值 {mv_total:,.2f} / "
                 f"累計已實現淨損益 {st.get('realized_pnl_net_twd', 0):+,.1f}"
                 f"（{st.get('wins',0)}勝{st.get('losses',0)}敗）")
    return "\n".join(lines), mv_total


def append_journal(text):
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write("\n" + text + "\n")


def push_line(text):
    token, uid = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"), os.environ.get("LINE_USER_ID")
    if not token or not uid:
        log("LINE secrets 未設定，跳過推播")
        return
    body = json.dumps({"to": uid, "messages": [{"type": "text", "text": text[:4900]}]}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            log(f"LINE 推播完成（{r.status}）")
    except Exception as e:  # noqa: BLE001
        log(f"LINE 推播失敗（不阻斷）：{e}")


def main():
    log(f"=== 模擬投資人 {TODAY} ===")
    with open(LEDGER_PATH, encoding="utf-8") as f:
        ledger = json.load(f)
    with open(JOURNAL_PATH, encoding="utf-8") as f:
        journal_tail = f.read()

    feed = fetch_feed()
    twse, twse_today = fetch_twse_closes()
    tpex, tpex_today = fetch_tpex_closes()
    prices_today = twse_today or tpex_today

    scan = (feed or {}).get("latest_scan") or {}
    feed_fresh = bool(feed) and parse_dt(scan.get("scanned_at")) == TODAY

    # 休市判定：feed 非今日且收盤價也非今日 → 一行日誌收工
    if not feed_fresh and not prices_today:
        log("feed 與收盤價皆非今日 → 休市或全面異常，本輪不動作")
        append_journal(f"### {TODAY:%Y/%m/%d}（GitHub Actions）— 休市/無今日資料，本輪不動作")
        ledger["state"]["last_run"] = f"{TODAY} 休市/無資料"
        with open(LEDGER_PATH, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
        return

    mode = "full" if feed_fresh else "risk_only"
    if mode == "risk_only":
        log("⚠️ feed 非今日但有收盤價 → 僅做持倉風控（可能 app 掃描異常）")

    wp = (feed or {}).get("watchpool") or {}
    wp_items = dedupe_watchpool(wp.get("results") or [])
    gate = wp.get("gate") or {}
    scan_results = scan.get("results") or []
    reviews = review_positions(ledger["positions"], twse, tpex)
    log(f"持倉 {len(ledger['positions'])} 檔檢視完成；準備池 {len(wp_items)} 筆（去重後）")

    rules_only = os.environ.get("RULES_ONLY") == "1" or not os.environ.get("ANTHROPIC_API_KEY")
    if rules_only:
        log("規則守門模式（零 API 費）：只做出場風控")
        mode = "rules_only"
        decision = rules_decision(reviews)
    else:
        state_md = http_get(STATE_MD_URL, retries=2)
        prompt = build_prompt(mode, ledger, reviews, wp_items, gate, scan_results, state_md, journal_tail)
        try:
            decision = call_claude(prompt)
        except Exception as e:  # noqa: BLE001
            log(f"❌ Claude 決策失敗：{e}，退回規則守門模式")
            mode = "rules_only"
            decision = rules_decision(reviews)
            decision["gate_text"] = f"（Claude 失敗退回規則守門：{type(e).__name__}）"

    exit_lines, entry_lines, warnings = apply_decisions(
        decision, ledger, reviews, wp_items, scan_results, twse, tpex, mode)
    entry_text, mv_total = render_journal(
        mode, decision, ledger, reviews, exit_lines, entry_lines, warnings, twse, tpex)
    append_journal(entry_text)

    ledger["state"]["last_run"] = (f"{TODAY} GHA {mode}：出場{len(exit_lines)}/進場{len(entry_lines)}，"
                                   f"現金 {ledger['cash']:,.0f}")
    with open(LEDGER_PATH, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)

    st = ledger["stats"]
    line_msg = (f"📒 模擬投資人 {TODAY:%m/%d}\n{decision.get('line_summary','')}\n"
                f"帳本：現金 {ledger['cash']:,.0f}／持倉市值 {mv_total:,.0f}／"
                f"已實現 {st.get('realized_pnl_net_twd',0):+,.0f}")
    push_line(line_msg)
    log("=== 完成 ===")
    print(entry_text)


if __name__ == "__main__":
    main()
