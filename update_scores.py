#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_scores.py — 从 football-data.co.uk 抓取 27 国 38 个联赛的当前赛季赛果，
输出 football_latest.json 与 football_latest.js（window.LATEST_DATA）供 index.html 合并增量使用。

  A 类（mmz4281 按赛季分文件）：抓当前赛季，404 回退上一赛季
  B 类（/new/{code}.csv 单一全历史文件）：解析后只取文件内最新赛季的已赛场次

用法: python3 update_scores.py
依赖: 仅标准库（urllib / csv / json / time）
"""

import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEAM_CN_PATH = os.path.join(BASE_DIR, "team_cn.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "football_latest.json")
OUTPUT_JS_PATH = os.path.join(BASE_DIR, "football_latest.js")

# A 类：mmz4281/{赛季码}/{联赛码}.csv
LEAGUES_A = ["E0", "SP1", "D1", "I1", "F1",
             "E1", "E2", "E3", "EC",
             "SC0", "SC1", "SC2", "SC3",
             "D2", "I2", "SP2", "F2",
             "N1", "B1", "P1", "T1", "G1"]
# B 类：new/{联赛码}.csv（全历史小文件）
LEAGUES_B = ["ARG", "AUT", "BRA", "CHN", "DNK", "FIN", "IRL", "JPN",
             "MEX", "NOR", "POL", "ROU", "RUS", "SWE", "SWZ", "USA"]
# B 类代码 404 时的备选代码
B_ALT_CODES = {"SWZ": "SUI"}

URL_TEMPLATE = "https://www.football-data.co.uk/mmz4281/{code}/{league}.csv"
URL_EXTRA = "https://www.football-data.co.uk/new/{league}.csv"
TIMEOUT = 40
SLEEP = 1.0
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) update_scores/2.0"

FTR_MAP = {"H": "B", "A": "P", "D": "T"}


def current_season_start(today=None):
    """当前赛季起始年：月份 >= 7 则为当年，否则为上年。"""
    today = today or datetime.now()
    return today.year if today.month >= 7 else today.year - 1


def season_code(start_year):
    """2025 -> '2526'"""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def download(url):
    """下载 CSV 文本；404 返回 None，其他异常抛出。"""
    time.sleep(SLEEP)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return raw.decode("utf-8-sig", errors="replace")


def parse_date(s):
    """dd/mm/yy 或 dd/mm/yyyy -> YYYY-MM-DD；失败返回 None。"""
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def to_int(s):
    try:
        return int((s or "").strip())
    except ValueError:
        return None


def make_match(date, home, s1, s2, away, res, season, league, team_cn, missing_teams):
    for team in (home, away):
        if team not in team_cn:
            team_cn[team] = team  # 先以英文名占位，方便后续补中文
            missing_teams.add(team)
    return {
        "date": date,
        "team1": home,
        "s1": s1,
        "s2": s2,
        "team2": away,
        "result": FTR_MAP[res],
        "team1_cn": team_cn.get(home, home),
        "team2_cn": team_cn.get(away, away),
        "season": str(season),
        "_league": league,
    }


def parse_main_csv(text, league, season_start, team_cn, missing_teams):
    """A 类 CSV（HomeTeam/AwayTeam/FTHG/FTAG/FTR），返回已赛场次列表。"""
    matches = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        home = (row.get("HomeTeam") or "").strip()
        away = (row.get("AwayTeam") or "").strip()
        ftr = (row.get("FTR") or "").strip().upper()
        date = parse_date(row.get("Date"))
        s1, s2 = to_int(row.get("FTHG")), to_int(row.get("FTAG"))
        if not home or not away or not date or ftr not in FTR_MAP:
            continue
        if s1 is None or s2 is None:
            continue
        matches.append(make_match(date, home, s1, s2, away, ftr,
                                  season_start, league, team_cn, missing_teams))
    return matches


def parse_extra_csv_latest(text, league, team_cn, missing_teams):
    """B 类 CSV（Season/Home/Away/HG/AG/Res），只取文件内最新赛季的已赛场次。
    赛季列可能是 "2025/2026"（跨年）或 "2026"（自然年），统一取起始年最大值。"""
    rows = []
    max_season = None
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        season_raw = (row.get("Season") or "").strip()
        try:
            start = int(season_raw[:4])
        except ValueError:
            continue
        home = (row.get("Home") or "").strip()
        away = (row.get("Away") or "").strip()
        res = (row.get("Res") or "").strip().upper()
        date = parse_date(row.get("Date"))
        s1, s2 = to_int(row.get("HG")), to_int(row.get("AG"))
        if not home or not away or not date or res not in FTR_MAP:
            continue
        if s1 is None or s2 is None:
            continue
        if max_season is None or start > max_season:
            max_season = start
        rows.append((start, date, home, s1, s2, away, res))
    if max_season is None:
        return []
    return [make_match(date, home, s1, s2, away, res, max_season,
                       league, team_cn, missing_teams)
            for (start, date, home, s1, s2, away, res) in rows
            if start == max_season]


def main():
    start_year = current_season_start()
    print(f"当前赛季起始年判定为 {start_year}（{season_code(start_year)} 赛季）")

    # 加载中文名映射
    if os.path.exists(TEAM_CN_PATH):
        with open(TEAM_CN_PATH, encoding="utf-8") as f:
            team_cn = json.load(f)
    else:
        team_cn = {}
        print(f"警告：未找到 {TEAM_CN_PATH}，将全部使用英文名")

    missing_teams = set()
    all_matches = []
    summary = {}

    # ---- A 类：当前赛季，404 回退上一赛季 ----
    for league in LEAGUES_A:
        text = None
        used_year = None
        for year in (start_year, start_year - 1):
            url = URL_TEMPLATE.format(code=season_code(year), league=league)
            try:
                text = download(url)
            except Exception as e:
                print(f"[{league}] {season_code(year)} 赛季下载失败：{e}，尝试回退...")
                text = None
            if text is not None:
                used_year = year
                if year != start_year:
                    print(f"[{league}] 当前赛季 CSV 不可用，已回退到 {season_code(year)} 赛季")
                break
            print(f"[{league}] {season_code(year)} 赛季 404，尝试回退...")

        if text is None:
            print(f"[{league}] 警告：当前与上一赛季均下载失败，跳过该联赛")
            summary[league] = 0
            continue

        matches = parse_main_csv(text, league, used_year, team_cn, missing_teams)
        all_matches.extend(matches)
        summary[league] = len(matches)
        print(f"[{league}] {season_code(used_year)} 赛季抓到 {len(matches)} 场已赛比赛")

    # ---- B 类：全历史文件，取最新赛季已赛场次 ----
    for league in LEAGUES_B:
        text = None
        codes = [league] + ([B_ALT_CODES[league]] if league in B_ALT_CODES else [])
        for c in codes:
            try:
                text = download(URL_EXTRA.format(league=c))
            except Exception as e:
                print(f"[{league}] 全历史文件下载失败：{e}")
                text = None
            if text is not None:
                if c != league:
                    print(f"[{league}] 主代码 404，已改用备选代码 {c}")
                break
            print(f"[{league}] new/{c}.csv 404")

        if text is None:
            print(f"[{league}] 警告：下载失败，跳过该联赛")
            summary[league] = 0
            continue

        matches = parse_extra_csv_latest(text, league, team_cn, missing_teams)
        all_matches.extend(matches)
        summary[league] = len(matches)
        season_label = matches[0]["season"] if matches else "?"
        print(f"[{league}] 最新赛季（{season_label}）抓到 {len(matches)} 场已赛比赛")

    # 有新增球队时回写 team_cn.json（不覆盖已有键）
    if missing_teams:
        with open(TEAM_CN_PATH, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(team_cn.items())), f, ensure_ascii=False, indent=2)
        print(f"\n以下 {len(missing_teams)} 支球队缺少中文名（已按英文名写入 team_cn.json，请补充）：")
        for t in sorted(missing_teams):
            print(f"  - {t}")

    all_matches.sort(key=lambda m: m["date"])

    output = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "matches": all_matches,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 同步输出 JS 版本（window.LATEST_DATA），供 file:// 方式直接打开页面时加载
    with open(OUTPUT_JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.LATEST_DATA = ")
        json.dump(output, f, ensure_ascii=False)
        f.write(";\n")

    print("\n===== 汇总 =====")
    for league in LEAGUES_A + LEAGUES_B:
        print(f"  {league}: {summary.get(league, 0)} 场")
    print(f"  合计: {len(all_matches)} 场")
    print(f"  输出文件: {OUTPUT_PATH}")
    print(f"  输出文件: {OUTPUT_JS_PATH}")


if __name__ == "__main__":
    sys.exit(main())
