#!/usr/bin/env python3
"""
FUN4YEAH 场次监控：盯指定日期（默认 2026-10-11），区分「出现但已满/未开报」和「真能报」，
通过飞书群机器人通知（Telegram / 企业微信可选）。纯标准库，无需 pip。

接口（公开、无需登录）：
  GET https://fun4yeah.com/tables/activities?limit=1000
  GET https://fun4yeah.com/tables/time_slots?limit=1000
前端判定逻辑（抄自 user.js）：
  剩余 = capacity - registered_count，<=0 即已满
  活动可报 = status=='published' 且 reg_start_date <= now <= reg_end_date（北京时间）
  场次已结束 = date + end_time < now

环境变量：
  FEISHU_WEBHOOK              飞书群自定义机器人 webhook（必填，完整 URL 或 hook/ 后面那串）
  FEISHU_SECRET               飞书机器人「签名校验」密钥（开了签名校验才填）
  TG_BOT_TOKEN / TG_CHAT_ID   Telegram 机器人（可选）
  WECOM_WEBHOOK               企业微信群机器人（可选）
用法：
  python3 fun4yeah_monitor.py                 # 常驻监控，启动时先推一条测试消息
  python3 fun4yeah_monitor.py --no-test       # 跳过启动测试推送
推送规则：
  - 任意日期出现新场次（新日期 / 老日期加场）→ 推一次，其中有可报的标紧急
    首次运行把现有场次记为基线，不推
  - 目标日期（--date）额外精细盯：出现 / 放出可报 / 抢光 / 定时重复提醒 / 消失
  python3 fun4yeah_monitor.py --once --dry-run --date 2026-09-26   # 测试一轮，只打印不推送
"""
import argparse
import base64
import hashlib
import hmac
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


def slot_key(s):
    """用 活动+日期+时段 当唯一键，而不是 id：后台删了重建同一场次不会误报成新场次"""
    return f"{s.get('activity_id')}|{s.get('date')}|{s.get('start_time')}-{s.get('end_time')}"


def snapshot_all():
    """返回 {日期: {slot_key: 场次信息}}，一轮只打两次接口"""
    now = datetime.now(CST)
    acts = {a["id"]: a for a in fetch_table("activities")}
    slots = sorted(fetch_table("time_slots"),
                   key=lambda s: (s.get("date", ""), s.get("start_time", "")))
    out = {}
    for s in slots:
        act = acts.get(s.get("activity_id"))
        state, why = slot_state(s, act, now)
        out.setdefault(s.get("date", "?"), {})[slot_key(s)] = {
            "state": state, "why": why,
            "time": f"{s.get('start_time')}-{s.get('end_time')}",
            "cap": s.get("capacity"), "reg": s.get("registered_count"),
            "act": (act or {}).get("title", s.get("activity_id")),
            "loc": (act or {}).get("location", ""),
        }
    return out


# ---------------- 通知 ----------------
# 每条消息都带这个前缀：飞书机器人如果设了「自定义关键词=场次」，所有消息都能命中
MSG_PREFIX = "【场次监控】"

FEISHU_ERR_HINT = {
    19024: "关键词不匹配：机器人安全设置里的关键词要能在消息里找到（消息都带「场次监控」，关键词设成「场次」即可）",
    19021: "签名校验失败：检查 FEISHU_SECRET 是否和机器人安全设置里的密钥一致、服务器时间是否准",
    19022: "IP 不在白名单：机器人安全设置里加上服务器公网 IP，或者关掉 IP 白名单",
    19001: "webhook 地址无效：检查 FEISHU_WEBHOOK 是否复制完整、机器人是否被删",
    9499: "请求太频繁被限流",
}


class Notifier:
    def __init__(self, dry):
        self.dry = dry
        self.feishu = os.getenv("FEISHU_WEBHOOK", "").strip()
        if self.feishu and not self.feishu.startswith("http"):   # 只填了 hook 后面那串也行
            self.feishu = f"https://open.feishu.cn/open-apis/bot/v2/hook/{self.feishu}"
        self.feishu_secret = os.getenv("FEISHU_SECRET", "").strip()   # 开了签名校验才需要
        # 以下为可选的备用渠道，不填就不发
        self.tg_token = os.getenv("TG_BOT_TOKEN", "")
        self.tg_chat = os.getenv("TG_CHAT_ID", "")
        self.wecom = os.getenv("WECOM_WEBHOOK", "")
        if self.wecom and not self.wecom.startswith("http"):
            self.wecom = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={self.wecom}"

        self.channels = []
        if self.feishu:
            self.channels.append(("飞书", self._feishu))
        if self.tg_token and self.tg_chat:
            self.channels.append(("Telegram", self._telegram))
        if self.wecom:
            self.channels.append(("企业微信", self._wecom))
        if not dry and not self.channels:
            sys.exit("没配通知渠道：至少设置 FEISHU_WEBHOOK（可选 TG_BOT_TOKEN+TG_CHAT_ID / WECOM_WEBHOOK）")

    # --- 各渠道：成功返回 None，失败返回错误描述 ---
    def _feishu(self, title, text, urgent):
        body = f"{title}\n\n{text}"
        if urgent:
            body = '<at user_id="all">所有人</at> ' + body   # 紧急消息 @所有人，手机强提醒
        payload = {"msg_type": "text", "content": {"text": body}}
        if self.feishu_secret:
            ts = str(int(time.time()))
            key = f"{ts}\n{self.feishu_secret}".encode()
            payload["timestamp"] = ts
            payload["sign"] = base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode()
        r = http_json(self.feishu, payload)
        code = r.get("code", r.get("StatusCode", -1))
        if code != 0:
            hint = FEISHU_ERR_HINT.get(code, "")
            return f"code={code} msg={r.get('msg') or r.get('StatusMessage')} {hint}".strip()
        return None

    def _telegram(self, title, text, urgent):
        r = http_json(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                      {"chat_id": self.tg_chat, "text": f"{title}\n\n{text}",
                       "disable_web_page_preview": True})
        return None if r.get("ok") else str(r)

    def _wecom(self, title, text, urgent):
        r = http_json(self.wecom, {"msgtype": "text", "text": {
            "content": f"{title}\n\n{text}",
            "mentioned_list": ["@all"] if urgent else []}})
        return None if r.get("errcode") == 0 else str(r)

    def send(self, title, text, urgent=False):
        """发到所有已配置渠道，返回 {渠道名: 错误或None}"""
        title = MSG_PREFIX + title
        log(f"[通知{'·紧急' if urgent else ''}] {title}\n{text}")
        results = {}
        if self.dry:
            return results
        for name, fn in self.channels:
            err = None
            for attempt in range(3):          # 网络抖动重试，最多 3 次
                try:
                    err = fn(title, text, urgent)
                except Exception as e:
                    err = f"请求异常: {e}"
                if err is None or not err.startswith("请求异常"):
                    break                     # 成功，或者是配置类错误（重试也没用）
                time.sleep(2 * (attempt + 1))
            results[name] = err
            if err:
                log(f"{name} 发送失败: {err}")
        return results

    def startup_test(self, date, interval):
        """启动时推一条测试消息，确认渠道通了；全部失败就直接退出，别让你以为在监控"""
        if self.dry:
            log("dry-run 模式，跳过启动测试推送")
            return
        text = (f"监控已启动 ✅ 这是一条测试消息\n"
                f"目标日期：{date}\n"
                f"轮询间隔：约 {interval} 秒\n"
                f"推送渠道：{'、'.join(n for n, _ in self.channels)}\n"
                f"时间：{datetime.now(CST):%Y-%m-%d %H:%M:%S}\n\n"
                f"收到这条说明推送正常。有新场次、或 {date} 可报时会再通知你。")
        results = self.send("推送测试", text)
        ok = [n for n, e in results.items() if e is None]
        bad = {n: e for n, e in results.items() if e is not None}
        if ok:
            log(f"启动测试推送成功：{'、'.join(ok)}")
        if bad and not ok:
            sys.exit("启动测试推送全部失败，脚本退出。检查上面的错误信息：\n"
                     + "\n".join(f"  {n}: {e}" for n, e in bad.items()))
        if bad:
            log(f"⚠️ 部分渠道失败（继续运行）：{bad}")


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
URL = f"{BASE}/activity/?from=gzh"


def check_new_slots(all_slots, nt, st, target):
    """任意日期出现新场次就推（目标日期由 check_target 单独负责，这里跳过避免重复推）"""
    cur_keys = {k for d in all_slots.values() for k in d}
    if "known" not in st:
        # 第一次运行：把现有场次当基线，不推，否则一启动就把 26、27 号全推一遍
        st["known"] = sorted(cur_keys)
        dates = ", ".join(sorted(all_slots))
        log(f"已记录现有场次作为基线（{len(cur_keys)} 个，日期：{dates}），之后新增才推送")
        return
    known = set(st["known"])
    new = cur_keys - known
    st["known"] = sorted(known | cur_keys)   # 只增不减：场次被删再加回来不会重复推
    if not new:
        return

    blocks, any_open, heads = [], False, []
    for date in sorted(all_slots):
        if date == target:
            continue
        ns = {k: v for k, v in all_slots[date].items() if k in new}
        if not ns:
            continue
        for v in ns.values():
            h = f"{v['act']}\n{v['loc']}"
            if h not in heads:
                heads.append(h)
        date_is_new = len(ns) == len(all_slots[date])
        opened = sum(v["state"] == OPEN for v in ns.values())
        any_open |= opened > 0
        blocks.append(f"【{date}】{'新日期 ' if date_is_new else '加场 '}{len(ns)}场，可报{opened}场\n"
                      + fmt(ns))
    if not blocks:
        return
    tag = "🔥新场次可报！" if any_open else "🆕新场次上线"
    nt.send(f"{tag} " + " / ".join(b.split("】")[0][1:] for b in blocks),
            "\n".join(heads) + "\n\n" + "\n\n".join(blocks) + f"\n\n{URL}",
            urgent=any_open)


def check_target(date, cur, nt, st, remind_every):
    """目标日期的精细盯梢：出现 / 放出可报 / 抢光 / 定时提醒 / 消失"""
    prev = st.get("slots", {})

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
        nt.send(f"{tag} {date} 场次上线", head + fmt(cur) + f"\n\n{URL}", urgent=bool(open_now))
        if open_now:
            st["last_remind"] = time.time()
    # 2) 新出现可报的场次（开报了 / 有人退了）
    elif open_now - open_prev:
        nt.send(f"🔥真能报！{date} 有场次放出", head + fmt(cur, {OPEN}) + f"\n\n赶紧去：{URL}",
                urgent=True)
        st["last_remind"] = time.time()
    # 3) 全部可报的都没了
    elif open_prev and not open_now:
        nt.send(f"❌ {date} 可报场次已被抢光", head + fmt(cur))
    # 4) 仍有可报场次，定时再提醒，防漏看
    elif open_now and remind_every > 0 and time.time() - st.get("last_remind", 0) > remind_every:
        nt.send(f"⏰还能报 {date}", head + fmt(cur, {OPEN}) + f"\n\n{URL}", urgent=True)
        st["last_remind"] = time.time()

    summary = {s: sum(v["state"] == s for v in cur.values()) for s in LABEL}
    log(f"{date} 共{len(cur)}场 " + " ".join(f"{LABEL[k]}{n}" for k, n in summary.items() if n))
    st["slots"] = cur


def run_once(date, nt, st, remind_every):
    all_slots = snapshot_all()
    check_new_slots(all_slots, nt, st, date)
    check_target(date, all_slots.get(date, {}), nt, st, remind_every)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-11")
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔秒，会加 ±15%% 抖动")
    ap.add_argument("--remind", type=int, default=600, help="有可报场次时重复提醒间隔秒，0=不重复")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    ap.add_argument("--no-test", action="store_true", help="启动时不推测试消息")
    args = ap.parse_args()

    nt = Notifier(args.dry_run)
    if not args.no_test:
        nt.startup_test(args.date, args.interval)
    st = {} if args.dry_run else load_state()
    if st.get("date") != args.date:
        # 换了目标日期：目标日期的状态清掉，已知场次基线保留
        st = {"date": args.date, **({"known": st["known"]} if "known" in st else {})}
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