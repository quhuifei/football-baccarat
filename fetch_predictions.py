#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_predictions.py — 从 API-Football 拉取未来 3 天比赛的模型预测，
输出 predictions.json 与 predictions.js（window.PREDICTIONS_DATA）供 predictions.html 使用。

  数据源：football_fixtures.js（window.FIXTURES_DATA，update_scores.py 生成，含 fid）
  窗口：今天 ~ 今天+3 天；优先级：五大联赛 → 其他 A 类主流联赛 → B 类联赛
  配额：每场 1 次 /predictions 调用，上限 45 次/运行，间隔 1 秒（Pro 7500/天足够）
  防空写：复用 update_scores.safe_write_output（空数据/异常少不覆盖旧文件）

用法: API_FOOTBALL_KEY=xxx python3 fetch_predictions.py
依赖: 仅标准库 + 同目录 update_scores.py（复用 safe_write_output）
"""

import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from update_scores import safe_write_output

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_JS_PATH = os.path.join(BASE_DIR, "football_fixtures.js")
OUTPUT_PATH = os.path.join(BASE_DIR, "predictions.json")
OUTPUT_JS_PATH = os.path.join(BASE_DIR, "predictions.js")

APIFB_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
APIFB_PRED_URL = "https://v3.football.api-sports.io/predictions?fixture={fid}"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) fetch_predictions/1.0"
TIMEOUT = 40
SLEEP = 1.0
MAX_CALLS = 45
DAYS_AHEAD = 3  # 今天 ~ 今天+3 天

# 联赛优先级：0=五大联赛，1=其他 A 类主流联赛，2=B 类联赛
_LEAGUE_PRIORITY = {}
for _c in ("E0", "SP1", "D1", "I1", "F1"):
    _LEAGUE_PRIORITY[_c] = 0
for _c in ("E1", "E2", "E3", "EC", "SC0", "SC1", "SC2", "SC3",
           "D2", "I2", "SP2", "F2", "N1", "B1", "P1", "T1", "G1"):
    _LEAGUE_PRIORITY[_c] = 1


def load_fixtures():
    """解析 football_fixtures.js（window.FIXTURES_DATA = {...};）。"""
    with open(FIXTURES_JS_PATH, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"window\.FIXTURES_DATA\s*=\s*(\{.*\})\s*;?\s*$", text, re.S)
    if not m:
        raise RuntimeError("football_fixtures.js 中未找到 FIXTURES_DATA")
    return (json.loads(m.group(1)) or {}).get("fixtures") or []


def pct_num(s):
    """'50%' -> 50；无法解析返回 None。"""
    try:
        return int(str(s).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def fetch_prediction(fid):
    req = urllib.request.Request(
        APIFB_PRED_URL.format(fid=fid),
        headers={"x-apisports-key": APIFB_KEY, "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def parse_prediction(fx, data):
    """把 API 返回压成页面需要的一行记录；关键字段缺失返回 None。"""
    resp = data.get("response") or []
    if not resp:
        return None
    r = resp[0]
    p = r.get("predictions") or {}
    percent = p.get("percent") or {}
    home_pct, draw_pct, away_pct = (pct_num(percent.get("home")),
                                    pct_num(percent.get("draw")),
                                    pct_num(percent.get("away")))
    if home_pct is None and draw_pct is None and away_pct is None:
        return None  # 模型未出概率，视为抓不到
    winner = p.get("winner") or {}
    teams = r.get("teams") or {}
    home_id = (teams.get("home") or {}).get("id")
    away_id = (teams.get("away") or {}).get("id")
    winner_side = None
    if winner.get("id") is not None:
        if winner.get("id") == home_id:
            winner_side = "home"
        elif winner.get("id") == away_id:
            winner_side = "away"
    goals = p.get("goals") or {}
    comp_raw = r.get("comparison") or {}
    comp = {}
    for key in ("form", "att", "def", "poisson_distribution", "h2h", "goals", "total"):
        c = comp_raw.get(key) or {}
        comp[key] = {"home": pct_num(c.get("home")), "away": pct_num(c.get("away"))}
    return {
        "fid": fx.get("fid"),
        "date": fx.get("date"), "time": fx.get("time") or "",
        "date_bj": fx.get("date_bj"), "time_bj": fx.get("time_bj") or "",
        "_league": fx.get("_league"),
        "team1": fx.get("team1"), "team2": fx.get("team2"),
        "team1_cn": fx.get("team1_cn") or fx.get("team1"),
        "team2_cn": fx.get("team2_cn") or fx.get("team2"),
        "winner_name": winner.get("name"),
        "winner_side": winner_side,          # home / away / None（模型没给胜方）
        "winner_comment": winner.get("comment"),
        "advice": p.get("advice"),
        "pct_home": home_pct, "pct_draw": draw_pct, "pct_away": away_pct,
        "goals_home": goals.get("home"), "goals_away": goals.get("away"),
        "under_over": p.get("under_over"),
        "comparison": comp,
    }


PRED_FIELDS = ("winner_name", "winner_side", "winner_comment", "advice",
               "pct_home", "pct_draw", "pct_away",
               "goals_home", "goals_away", "under_over", "comparison")


def reuse_record(fx, old):
    """复用旧 predictions.json 中同 fid 的预测字段，赛程信息（含北京时间）用新 fixtures 刷新。"""
    rec = {
        "fid": fx.get("fid"),
        "date": fx.get("date"), "time": fx.get("time") or "",
        "date_bj": fx.get("date_bj"), "time_bj": fx.get("time_bj") or "",
        "_league": fx.get("_league"),
        "team1": fx.get("team1"), "team2": fx.get("team2"),
        "team1_cn": fx.get("team1_cn") or fx.get("team1"),
        "team2_cn": fx.get("team2_cn") or fx.get("team2"),
    }
    rec.update({k: old.get(k) for k in PRED_FIELDS})
    return rec


def load_existing():
    """读已有 predictions.json，返回 {fid: 记录}；没有/损坏返回 {}。"""
    if not os.path.exists(OUTPUT_PATH):
        return {}
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            items = (json.load(f) or {}).get("items") or []
        return {it["fid"]: it for it in items if it.get("fid") is not None}
    except (ValueError, OSError):
        return {}


def main():
    if not APIFB_KEY:
        print("错误：未设置 API_FOOTBALL_KEY")
        return 1
    today = datetime.now(timezone.utc).date()
    last_day = today + timedelta(days=DAYS_AHEAD)

    fixtures = load_fixtures()
    window = [fx for fx in fixtures
              if fx.get("fid") and fx.get("date")
              and today <= datetime.strptime(fx["date"], "%Y-%m-%d").date() <= last_day]
    window.sort(key=lambda fx: (_LEAGUE_PRIORITY.get(fx.get("_league"), 2),
                                fx["date"], fx.get("time") or "99:99"))
    picked = window[:MAX_CALLS]
    print(f"窗口 {today} ~ {last_day} 内带 fid 的比赛 {len(window)} 场，"
          f"按联赛优先级取前 {len(picked)} 场（上限 {MAX_CALLS}）")

    existing = load_existing()  # 复用旧预测：同 fid 不再调 API，只刷新赛程字段
    items, failed, reused, calls = [], 0, 0, 0
    for i, fx in enumerate(picked, 1):
        label = f"{fx['_league']} {fx.get('team1_cn') or fx['team1']} vs {fx.get('team2_cn') or fx['team2']}"
        old = existing.get(fx.get("fid"))
        if old is not None:
            items.append(reuse_record(fx, old))
            reused += 1
            print(f"[{i}/{len(picked)}] {label} → 复用旧预测（fid 命中，未调 API）")
            continue
        time.sleep(SLEEP)
        calls += 1
        try:
            data = fetch_prediction(fx["fid"])
        except Exception as e:
            print(f"[{i}/{len(picked)}] {label} 请求失败：{e}，跳过")
            failed += 1
            continue
        if data.get("errors"):
            print(f"[{i}/{len(picked)}] {label} API 返回错误 {data['errors']}，跳过")
            failed += 1
            continue
        rec = parse_prediction(fx, data)
        if rec is None:
            print(f"[{i}/{len(picked)}] {label} 无预测数据，跳过")
            failed += 1
            continue
        items.append(rec)
        print(f"[{i}/{len(picked)}] {label} → 主{rec['pct_home']}% 平{rec['pct_draw']}% "
              f"客{rec['pct_away']}%，预测 {rec['winner_name'] or '-'}")

    items.sort(key=lambda x: (x["date"], x["time"] or "99:99"))
    output = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "items": items,
    }
    written = safe_write_output(OUTPUT_PATH, OUTPUT_JS_PATH,
                                "PREDICTIONS_DATA", output, "items", "比赛预测")
    print(f"\n===== 比赛预测 =====")
    print(f"  成功 {len(items)} 场（复用 {reused}，新抓 {len(items) - reused}），"
          f"跳过 {failed} 场，API 调用 {calls} 次")
    print(f"  输出文件: {OUTPUT_PATH}{'' if written else '（防空写：保留旧文件）'}")
    print(f"  输出文件: {OUTPUT_JS_PATH}{'' if written else '（防空写：保留旧文件）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
