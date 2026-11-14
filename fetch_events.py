#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取公开数据源 -> 生成 baseline.js
前端 index.html 每 30 秒轮询 baseline.js，版本号变化即弹窗通知。
"""

import json
import os
import re
import hashlib
import subprocess
from datetime import datetime, timezone, timedelta

import requests

TZ8 = timezone(timedelta(hours=8))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MonitorBot/1.0)"}
WEBHOOK = os.environ.get("ALERT_WEBHOOK", "").strip()  # 可选：飞书/钉钉/Telegram

REGIONS = ["上海", "江苏", "浙江", "安徽", "江西"]
TYPE_MAP = [
    (["火", "燃", "爆", "煙"], "fire"),
    (["殺", "刺", "傷", "盗", "詐欺", "逮捕", "刑事", "強盗"], "crime"),
    (["雨", "風", "霧", "雷", "雪", "寒潮", "高温", "洪水", "台風", "気象"], "disaster"),
    (["事故", "衝突", "追突", "横転", "高速", "渋滞", "炎上"], "accident"),
]


def now_iso():
    return datetime.now(TZ8).isoformat(timespec="seconds")


def today():
    return datetime.now(TZ8).strftime("%Y-%m-%d")


def make_id(*parts):
    raw = "|".join(str(p) for p in parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def guess_type(text):
    for keys, t in TYPE_MAP:
        if any(k in text for k in keys):
            return t
    return "accident"


def guess_region(text):
    for r in REGIONS:
        if r in text:
            return r
    return "上海"


def fetch(url, params=None, timeout=15):
    r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


# ---------- 数据源 1：中央气象台预警 ----------
def fetch_nmc_alerts():
    events = []
    try:
        r = fetch("http://www.nmc.cn/rest/findAlarm", params={"pageNo": 1, "pageSize": 30})
        data = r.json()
        items = data.get("data", {}).get("page", {}).get("list") or data.get("data", []) or []
        for it in items[:20]:
            title = it.get("title") or it.get("alerttitle") or ""
            if not title:
                continue
            region = guess_region(title)
            etype = guess_type(title)
            pid = it.get("alertid") or it.get("id") or title
            events.append({
                "id": make_id("nmc", pid),
                "type": etype,
                "region": region,
                "region_name": region,
                "title": title[:60],
                "detail": (it.get("content") or it.get("alertcontent") or title)[:200],
                "source": "中央気象台",
                "source_url": "http://www.nmc.cn/publish/alarm.html",
                "src_time": today(),
                "status": "予報",
                "is_new": True,
                "resolved": False,
            })
    except Exception as e:
        print(f"[warn] nmc failed: {e}")
    return events


# ---------- 数据源 2：中国地震台网 ----------
def fetch_earthquake():
    events = []
    try:
        r = fetch("https://news.ceic.ac.cn/ajax/google", timeout=15)
        m = re.findall(r"[\d.]+\|[\d.]+\|[^\|]+\|[\d.]+\|[\d.]+\|([^\|]+)\|([^\|]+)\|([^\|]+)", r.text)
        for depth, place, tm in m[:5]:
            events.append({
                "id": make_id("eq", place, tm),
                "type": "disaster",
                "region": guess_region(place),
                "region_name": guess_region(place),
                "title": f"地震: {place}",
                "detail": f"発生時刻 {tm}／深さ {depth}km（中国地震台網）",
                "source": "中国地震台網",
                "source_url": "https://news.ceic.ac.cn/",
                "src_time": tm[:10] if len(tm) >= 10 else today(),
                "status": "観測",
                "is_new": True,
                "resolved": False,
            })
    except Exception as e:
        print(f"[warn] earthquake failed: {e}")
    return events


# ---------- 主流程 ----------
def load_old_events():
    path = "baseline.js"
    if not os.path.exists(path):
        return []
    try:
        text = open(path, encoding="utf-8").read()
        start = text.index("[")
        arr = json.loads(text[start:text.rindex("]") + 1])
        # 旧事件保留 resolved 状态，只更新数据类字段
        return arr
    except Exception as e:
        print(f"[warn] parse old baseline failed: {e}")
        return []


def merge(old, fresh):
    old_map = {e["id"]: e for e in old}
    out = []
    for e in fresh:
        if e["id"] in old_map:
            # 已存在：保持 resolved 状态，取消 is_new
            o = old_map[e["id"]]
            e["is_new"] = False
            e["resolved"] = o.get("resolved", False)
        out.append(e)
    # 保留旧的手工事件（id 不以 nmc/eq 开头的）
    for o in old:
        if not o["id"].startswith(("nmc", "eq")):
            out.append(o)
    return out


def write_baseline(events):
    payload = {
        "version": datetime.now(TZ8).strftime("%Y%m%d.%H%M%S"),
        "generated_at": now_iso(),
        "events": events,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=None)
    open("baseline.js", "w", encoding="utf-8").write("window.BASELINE = " + body + ";\n")
    return payload


def notify_new(event):
    if not WEBHOOK:
        return
    text = f"🚨 {event['region_name']}｜{event['title']}\n{event['detail'][:120]}"
    try:
        if "feishu" in WEBHOOK or "lark" in WEBHOOK:
            requests.post(WEBHOOK, json={"msg_type": "text", "content": {"text": text}}, timeout=10)
        elif "dingtalk" in WEBHOOK:
            requests.post(WEBHOOK, json={"msgtype": "text", "text": {"content": text}}, timeout=10)
        else:
            requests.post(WEBHOOK, json={"text": text}, timeout=10)
    except Exception as e:
        print(f"[warn] webhook failed: {e}")


def main():
    print(f"=== fetch start {now_iso()} ===")
    fresh = fetch_nmc_alerts() + fetch_earthquake()
    print(f"fresh events: {len(fresh)}")

    old = load_old_events()
    merged = merge(old, fresh)

    # 找出真正新增的，触发 webhook
    old_ids = {e["id"] for e in old}
    for e in merged:
        if e["id"] not in old_ids:
            notify_new(e)

    payload = write_baseline(merged)
    print(f"version: {payload['version']}, total: {len(merged)}")

    # git commit & push（仅在有变化时）
    subprocess.run(["git", "config", "user.name", "monitor-bot"], check=False)
    subprocess.run(["git", "config", "user.email", "bot@users.noreply.github.com"], check=False)
    subprocess.run(["git", "add", "baseline.js"], check=False)
    r = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
    if r.returncode == 0:
        print("no change, skip commit")
        return
    subprocess.run(["git", "commit", "-m", f"data: auto update {payload['version']}"], check=False)
    subprocess.run(["git", "push"], check=False)
    print("pushed")


if __name__ == "__main__":
    main()
