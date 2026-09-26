#!/usr/bin/env python3
"""
FUN4YEAH 场次监控：盯指定日期（默认 2026-10-11），区分「出现但已满/未开报」和「真能报」，
通过 Telegram + PushPlus 通知。纯标准库，无需 pip。

接口（公开、无需登录）：
  GET https://fun4yeah.com/tables/activities?limit=1000
  GET https://fun4yeah.com/tables/time_slots?limit=1000
前端判定逻辑（抄自 user.js）：
  剩余 = capacity - registered_count，<=0 即已满
  活动可报 = status=='published' 且 reg_start_date <= now <= reg_end_date（北京时间）
  场次已结束 = date + end_time < now

环境变量：
  TG_BOT_TOKEN / TG_CHAT_ID   Telegram 机器人
  PUSHPLUS_TOKEN              PushPlus token
用法：
  python3 fun4yeah_monitor.py                 # 常驻监控
  python3 fun4yeah_monitor.py --once --dry-run --date 2026-09-26   # 测试一轮，只打印不推送
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://fun4yeah.com"
CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.47")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fun4yeah_state.json")

OPEN, FULL, NOT_OPEN, PASSED = "open", "full", "not_open", "passed"
LABEL = {OPEN: "✅可报", FULL: "❌已满", NOT_OPEN: "⏳未开报", PASSED: "⌛已结束"}


# ---------------- HTTP ----------------
def http_json(url, data=None, timeout=15):
    headers = {"User-Agent": UA, "Accept": "application/json",
               "Referer": f"{BASE}/activity/?from=gzh"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_table(table):
    j = http_json(f"{BASE}/tables/{table}?limit=1000")
    return [r for r in j.get("data", []) if not r.get("deleted")]


# ---------------- 判定 ----------------
def parse_act_ts(s, end_of_day):
    if not s:
        return None
    try:
        if len(s) == 10:
            t = "23:59:59" if end_of_day else "00:00:00"
            return datetime.fromisoformat(f"{s}T{t}").replace(tzinfo=CST)
        return datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=CST)
    except ValueError:
        return None


def activity_state(act, now):
    if not act:
        return False, "活动不存在/已删除"
    if act.get("status") != "published":
        return False, f"活动状态={act.get('status')}"
    st = parse_act_ts(act.get("reg_start_date"), False)
    ed = parse_act_ts(act.get("reg_end_date"), True)
    if st and now < st:
        return False, f"{act.get('reg_start_date')} 开始报名"
    if ed and now > ed:
        return False, "报名已截止"
    return True, "报名中"


def slot_state(slot, act, now):
    try:
        end = datetime.fromisoformat(f"{slot['date']}T{slot['end_time']}").replace(tzinfo=CST)
        if end < now:
            return PASSED, "场次已结束"
    except (KeyError, ValueError):
        pass
    if not slot.get("capacity"):
        return NOT_OPEN, "场次已建但未放名额"
    remain = (slot.get("capacity") or 0) - (slot.get("registered_count") or 0)
    if remain <= 0:
        return FULL, "名额已满"
    ok, why = activity_state(act, now)
    if not ok:
        return NOT_OPEN, why   # 有余量但活动还没开报 / 已截止
    return OPEN, f"剩 {remain}"


def snapshot(target_date):
    now = datetime.now(CST)
    acts = {a["id"]: a for a in fetch_table("activities")}
    slots = [s for s in fetch_table("time_slots") if s.get("date") == target_date]
    slots.sort(key=lambda s: s.get("start_time", ""))
    out = {}
    for s in slots:
        act = acts.get(s.get("activity_id"))
        state, why = slot_state(s, act, now)
        out[s["id"]] = {
            "state": state, "why": why,
            "time": f"{s.get('start_time')}-{s.get('end_time')}",
            "cap": s.get("capacity"), "reg": s.get("registered_count"),
            "act": (act or {}).get("title", s.get("activity_id")),
            "loc": (act or {}).get("location", ""),
        }
    return out


# ---------------- 通知 ----------------
class Notifier:
    def __init__(self, dry):
        self.dry = dry
        self.tg_token = os.getenv("TG_BOT_TOKEN", "")
        self.tg_chat = os.getenv("TG_CHAT_ID", "")
        self.pp_token = os.getenv("PUSHPLUS_TOKEN", "")
        if not dry and not ((self.tg_token and self.tg_chat) or self.pp_token):
            sys.exit("没配通知渠道：设置 TG_BOT_TOKEN+TG_CHAT_ID 和/或 PUSHPLUS_TOKEN")

    def send(self, title, text):
        log(f"[通知] {title}\n{text}")
        if self.dry:
            return
        if self.tg_token and self.tg_chat:
            try:
                http_json(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                          {"chat_id": self.tg_chat, "text": f"{title}\n\n{text}",
                           "disable_web_page_preview": True})
            except Exception as e:
                log(f"Telegram 发送失败: {e}")
        if self.pp_token:
            try:
                r = http_json("https://www.pushplus.plus/send",
                              {"token": self.pp_token, "title": title,
                               "content": text.replace("\n", "<br>"), "template": "html"})
                if r.get("code") != 200:
                    log(f"PushPlus 返回异常: {r}")
            except Exception as e:
                log(f"PushPlus 发送失败: {e}")


def fmt(slots, only=None):
    lines = []
    for v in slots.values():
        if only and v["state"] not in only:
            continue
        lines.append(f"{v['time']} {LABEL[v['state']]} ({v['reg']}/{v['cap']}) {v['why']}")
    return "\n".join(lines)


def log(msg):
    print(f"[{datetime.now(CST):%m-%d %H:%M:%S}] {msg}", flush=True)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, ensure_ascii=False)


# ---------------- 主循环 ----------------
def run_once(date, nt, st, remind_every):
    cur = snapshot(date)
    prev = st.get("slots", {})
    url = f"{BASE}/activity/?from=gzh"

    if not cur:
        log(f"{date} 暂无场次")
        if prev:
            nt.send(f"⚠️ {date} 场次消失了", "之前出现过的场次现在接口里没了（被删/改期？）")
        st["slots"] = {}
        return

    act = next(iter(cur.values()))
    head = f"{act['act']}\n{act['loc']}\n"
    open_now = {k for k, v in cur.items() if v["state"] == OPEN}
    open_prev = {k for k, v in prev.items() if v["state"] == OPEN}

    # 1) 日期首次出现（不管能不能报都告诉你一声）
    if not prev:
        tag = "🔥真能报！" if open_now else "👀日期出现了（暂不可报）"
        nt.send(f"{tag} {date} 场次上线", head + fmt(cur) + f"\n\n{url}")
        if open_now:
            st["last_remind"] = time.time()
    # 2) 新出现可报的场次（开报了 / 有人退了）
    elif open_now - open_prev:
        nt.send(f"🔥真能报！{date} 有场次放出", head + fmt(cur, {OPEN}) + f"\n\n赶紧去：{url}")
        st["last_remind"] = time.time()
    # 3) 全部可报的都没了
    elif open_prev and not open_now:
        nt.send(f"❌ {date} 可报场次已被抢光", head + fmt(cur))
    # 4) 仍有可报场次，定时再提醒，防漏看
    elif open_now and remind_every > 0 and time.time() - st.get("last_remind", 0) > remind_every:
        nt.send(f"⏰还能报 {date}", head + fmt(cur, {OPEN}) + f"\n\n{url}")
        st["last_remind"] = time.time()

    summary = {s: sum(v["state"] == s for v in cur.values()) for s in LABEL}
    log(f"{date} 共{len(cur)}场 " + " ".join(f"{LABEL[k]}{n}" for k, n in summary.items() if n))
    st["slots"] = cur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-11")
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔秒，会加 ±15%% 抖动")
    ap.add_argument("--remind", type=int, default=600, help="有可报场次时重复提醒间隔秒，0=不重复")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    args = ap.parse_args()

    nt = Notifier(args.dry_run)
    st = {} if args.dry_run else load_state()
    if st.get("date") != args.date:
        st = {"date": args.date}
    fails = 0
    log(f"开始监控 {args.date}，间隔约 {args.interval}s")

    while True:
        try:
            run_once(args.date, nt, st, args.remind)
            if fails >= 5:
                nt.send("✅ 监控恢复", f"接口恢复正常（之前连续失败 {fails} 次）")
            fails = 0
            if not args.dry_run:
                save_state(st)
        except Exception as e:
            fails += 1
            log(f"请求失败 #{fails}: {e}")
            if fails == 5:
                nt.send("⚠️ 监控异常", f"连续 {fails} 次请求失败：{e}\n可能网站改版/挂了/IP 被限")
        if args.once:
            break
        base = args.interval * (1 + min(fails, 5)) if fails else args.interval  # 失败时退避
        time.sleep(base * random.uniform(0.85, 1.15))


if __name__ == "__main__":
    main()
