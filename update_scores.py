#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_scores.py — 从 football-data.co.uk 抓取五大联赛当前赛季赛果，
输出 football_latest.json 供 index.html 合并增量使用。

用法: python3 update_scores.py
依赖: 仅标准库（urllib / csv / json）
"""

import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEAM_CN_PATH = os.path.join(BASE_DIR, "team_cn.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "football_latest.json")

LEAGUES = ["E0", "SP1", "D1", "I1", "F1"]
URL_TEMPLATE = "https://www.football-data.co.uk/mmz4281/{code}/{league}.csv"
TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) update_scores/1.0"

FTR_MAP = {"H": "B", "A": "P", "D": "T"}


def current_season_start(today=None):
    """当前赛季起始年：月份 >= 7 则为当年，否则为上年。"""
    today = today or datetime.now()
    return today.year if today.month >= 7 else today.year - 1


def season_code(start_year):
    """2025 -> '2526'"""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def fetch_csv(league, start_year):
    """下载指定联赛指定赛季 CSV，返回文本；失败抛异常。"""
    url = URL_TEMPLATE.format(code=season_code(start_year), league=league)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
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


def parse_csv(text, league, season_start, team_cn, missing_teams):
    """解析 CSV，返回该联赛已赛场次列表。"""
    matches = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        home = (row.get("HomeTeam") or "").strip()
        away = (row.get("AwayTeam") or "").strip()
        ftr = (row.get("FTR") or "").strip().upper()
        fthg = (row.get("FTHG") or "").strip()
        ftag = (row.get("FTAG") or "").strip()
        date = parse_date(row.get("Date"))

        # 跳过无比分或未赛的行
        if not home or not away or not date or ftr not in FTR_MAP:
            continue
        if not fthg or not ftag:
            continue
        try:
            s1, s2 = int(fthg), int(ftag)
        except ValueError:
            continue

        for team in (home, away):
            if team not in team_cn:
                team_cn[team] = team  # 先以英文名占位，方便用户后续补中文
                missing_teams.add(team)

        matches.append({
            "date": date,
            "team1": home,
            "s1": s1,
            "s2": s2,
            "team2": away,
            "result": FTR_MAP[ftr],
            "team1_cn": team_cn.get(home, home),
            "team2_cn": team_cn.get(away, away),
            "season": str(season_start),
            "_league": league,
        })
    return matches


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

    for league in LEAGUES:
        text = None
        used_year = None
        # 先试当前赛季，404/失败则回退上一赛季
        for year in (start_year, start_year - 1):
            try:
                text = fetch_csv(league, year)
                used_year = year
                if year != start_year:
                    print(f"[{league}] 当前赛季 CSV 不可用，已回退到 {season_code(year)} 赛季")
                break
            except urllib.error.HTTPError as e:
                print(f"[{league}] {season_code(year)} 赛季 HTTP {e.code}，尝试回退...")
                continue
            except Exception as e:
                print(f"[{league}] {season_code(year)} 赛季下载失败：{e}，尝试回退...")
                continue

        if text is None:
            print(f"[{league}] 警告：当前与上一赛季均下载失败，跳过该联赛")
            summary[league] = 0
            continue

        matches = parse_csv(text, league, used_year, team_cn, missing_teams)
        all_matches.extend(matches)
        summary[league] = len(matches)
        print(f"[{league}] {season_code(used_year)} 赛季抓到 {len(matches)} 场已赛比赛")

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

    print("\n===== 汇总 =====")
    for league in LEAGUES:
        print(f"  {league}: {summary.get(league, 0)} 场")
    print(f"  合计: {len(all_matches)} 场")
    print(f"  输出文件: {OUTPUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
