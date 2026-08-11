#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_scores.py — 从 football-data.co.uk 抓取 27 国 38 个联赛的当前赛季赛果，
输出 football_latest.json 与 football_latest.js（window.LATEST_DATA）供 index.html 合并增量使用。
另从 A 类当前赛季 CSV 中导出「比分为空」的已排期场次，输出 football_fixtures.json /
football_fixtures.js（window.FIXTURES_DATA），即今天起未来 7 天的近期赛程。

  A 类（mmz4281 按赛季分文件）：抓当前赛季，404 回退上一赛季
  B 类（/new/{code}.csv 单一全历史文件）：解析后只取文件内最新赛季的已赛场次

快速通道（可选）：football-data.co.uk 赛果更新滞后 1~3 天，设置环境变量
API_FOOTBALL_KEY 后，脚本会再从 API-Football（api-sports.io）按天拉取全球比赛，
过滤出本站 38 个联赛的已赛场次补最新赛果，并把窗口内未开赛（NS）的场次导出为
近期赛程（默认拉 昨天+今天+明天 共 3 天，多拉的「明天」专为赛程；
免费版 100 次/天且只开放 [昨天, 明天] 三天窗口，3×24=72 次/天够用；
升级 Pro 后把 FAST_DAYS 调大即可回溯更多天）。未设置该变量时脚本行为与旧版完全一致。
队名经 team_aliases.py 显式映射回 football-data.co.uk 短名，未映射的球队整场跳过
（宁可不更新，也不制造同一场比赛的重复记录）。

注意：football-data.co.uk 在新赛季 CSV 未就绪时会把缺失文件 301 重定向到别的联赛
文件（如 E0.csv→EC.csv），本脚本对跨路径重定向一律视为文件不存在并回退上一赛季，
防止一场比赛被打上多个联赛标签。

用法: python3 update_scores.py
依赖: 仅标准库（urllib / csv / json / time）+ 同目录 team_aliases.py
"""

import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from team_aliases import TEAM_ALIASES
except ImportError:
    TEAM_ALIASES = {}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEAM_CN_PATH = os.path.join(BASE_DIR, "team_cn.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "football_latest.json")
OUTPUT_JS_PATH = os.path.join(BASE_DIR, "football_latest.js")
FIXTURES_PATH = os.path.join(BASE_DIR, "football_fixtures.json")
FIXTURES_JS_PATH = os.path.join(BASE_DIR, "football_fixtures.js")
FIXTURES_DAYS = 7  # 近期赛程窗口：今天起未来 7 天

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

# ---- 快速通道：API-Football（api-sports.io）----
APIFB_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
APIFB_URL = "https://v3.football.api-sports.io/fixtures?date={d}"
FAST_DAYS = 2      # 拉取 昨天+今天（免费版只开放 [昨天, 明天] 窗口且 100 次/天：2×24=48 次/天；升级 Pro 后调大可回溯更多天）
APIFB_SLEEP = 6    # 调用间隔秒数（免费版限流保护）
# API-Football league.id → 本站联赛码（2026-08 逐一用 /leagues 接口按 国家+名称+League 类型核实）
APIFB_LEAGUES = {
    39: "E0", 40: "E1", 41: "E2", 42: "E3", 43: "EC",
    140: "SP1", 141: "SP2", 78: "D1", 79: "D2",
    135: "I1", 136: "I2", 61: "F1", 62: "F2",
    179: "SC0", 180: "SC1", 183: "SC2", 184: "SC3",
    88: "N1", 144: "B1", 94: "P1", 203: "T1", 197: "G1",
    128: "ARG", 218: "AUT", 71: "BRA", 169: "CHN", 119: "DNK",
    244: "FIN", 357: "IRL", 98: "JPN", 262: "MEX", 103: "NOR",
    106: "POL", 283: "ROU", 235: "RUS", 113: "SWE", 207: "SWZ", 253: "USA",
}
# 本站联赛码 → 联赛当地相对 UTC 的固定偏移小时（用于把 fixture.date 换算成联赛当地日期，
# 与 football-data.co.uk CSV 的当地日期惯例对齐。不追夏令时，±1 天误差由「同队 ±2 天吸附」兜底）
LEAGUE_UTC_OFFSET = {
    "E0": 0, "E1": 0, "E2": 0, "E3": 0, "EC": 0, "IRL": 0, "P1": 0,
    "SC0": 0, "SC1": 0, "SC2": 0, "SC3": 0,
    "SP1": 1, "SP2": 1, "D1": 1, "D2": 1, "I1": 1, "I2": 1, "F1": 1, "F2": 1,
    "N1": 1, "B1": 1, "AUT": 1, "POL": 1, "DNK": 1, "NOR": 1, "SWE": 1, "SWZ": 1,
    "G1": 2, "ROU": 2, "FIN": 2,
    "T1": 3, "RUS": 3,
    "ARG": -3, "BRA": -3, "MEX": -6, "USA": -5,
    "CHN": 8, "JPN": 9,
}


def current_season_start(today=None):
    """当前赛季起始年：月份 >= 7 则为当年，否则为上年。"""
    today = today or datetime.now()
    return today.year if today.month >= 7 else today.year - 1


def season_code(start_year):
    """2025 -> '2526'"""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


class _NoCrossPathRedirect(urllib.request.HTTPRedirectHandler):
    """football-data.co.uk 在新赛季文件未就绪时会把缺失文件 301 到别的联赛 CSV
    （如 2627/E0.csv → 2627/EC.csv，曾致一场比赛被标上 E0/E3/EC 三个联赛）。
    跨路径重定向一律拒绝跟随，按「文件不存在」处理并回退上一赛季。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_path = urllib.parse.urlparse(req.full_url).path
        new_path = urllib.parse.urlparse(newurl).path
        if new_path != old_path:
            return None  # urllib 将抛出 HTTPError(code)，download() 按不存在处理
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_NoCrossPathRedirect)


def download(url):
    """下载 CSV 文本；404/跨路径重定向/非 CSV 内容（HTML 错误页）返回 None，其他异常抛出。"""
    time.sleep(SLEEP)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404 or 300 <= e.code < 400:
            return None
        raise
    text = raw.decode("utf-8-sig", errors="replace")
    if text.lstrip().startswith("<"):  # HTML 错误页，不是 CSV
        return None
    return text


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


def make_fixture(date, time_s, home, away, season, league, team_cn):
    """未来赛程记录；新球队同样先以英文名占位（通常已赛球队已在 team_cn 中）。"""
    for team in (home, away):
        if team not in team_cn:
            team_cn[team] = team
    return {
        "date": date,
        "time": time_s,
        "team1": home,
        "team2": away,
        "team1_cn": team_cn.get(home, home),
        "team2_cn": team_cn.get(away, away),
        "season": str(season),
        "_league": league,
    }


def parse_main_fixtures(text, league, season_start, team_cn, today):
    """A 类 CSV 中比分为空（FTHG 无值）的已排期行 = 未来赛程。
    只保留 [今天, 今天+FIXTURES_DAYS] 窗口内的场次。B 类 /new/*.csv 只含已赛场次，
    经核实文件末尾即为最新已赛日期、无未来行，故未来赛程仅取 A 类。"""
    last_day = today + timedelta(days=FIXTURES_DAYS)
    fixtures = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        home = (row.get("HomeTeam") or "").strip()
        away = (row.get("AwayTeam") or "").strip()
        date = parse_date(row.get("Date"))
        if not home or not away or not date:
            continue
        if to_int(row.get("FTHG")) is not None:
            continue  # 已赛
        d = datetime.strptime(date, "%Y-%m-%d").date()
        if not (today <= d <= last_day):
            continue
        time_s = (row.get("Time") or "").strip()
        fixtures.append(make_fixture(date, time_s, home, away,
                                     season_start, league, team_cn))
    return fixtures


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


def fetch_apifb_fast(csv_index, team_cn, missing_teams):
    """快速通道：API-Football 按天拉全球比赛，过滤出本站 38 联赛的比赛。

    csv_index: {(联赛码, 主队, 客队): {CSV 已有日期}} —— 用于「同队 ±2 天吸附」兜底。
    单日请求失败只警告不中断；队名未映射整场跳过。返回 (赛果列表, 赛程列表)。
    赛果只收 FT（完场）；AET/PEN 按惯例取常规时间比分（score.fulltime）。
    赛程收 NS（未开赛）：免费版窗口含明天，故额外多拉 1 天（FAST_DAYS+1 次调用），
    得出「今天+明天」的近期赛程；CSV 一旦出现排期行会与之按同键去重合并。
    """
    today = datetime.now(timezone.utc).date()
    dates = [(today - timedelta(days=FAST_DAYS - 1 - i)) for i in range(FAST_DAYS)]
    dates.append(today + timedelta(days=1))  # 免费版窗口含明天，多拉 1 天补近期赛程
    start_year = current_season_start()
    out = []
    out_fixtures = []
    for day in dates:
        time.sleep(APIFB_SLEEP)
        req = urllib.request.Request(
            APIFB_URL.format(d=day.isoformat()),
            headers={"x-apisports-key": APIFB_KEY, "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.load(resp)
        except Exception as e:
            print(f"[快速通道 {day}] 警告：请求失败（{e}），跳过该天，保留 CSV 结果")
            continue
        if data.get("errors"):
            print(f"[快速通道 {day}] 警告：API 返回错误 {data['errors']}，跳过该天")
            continue

        added = added_fx = skipped_league = skipped_team = 0
        for f in data.get("response", []):
            lid = (f.get("league") or {}).get("id")
            league = APIFB_LEAGUES.get(lid)
            if not league:
                skipped_league += 1  # 杯赛/其他联赛，不属于本站 38 联赛
                continue
            status = (f.get("fixture") or {}).get("status", {}).get("short")
            utc = (f.get("fixture") or {}).get("date") or ""
            home_org = (f.get("teams") or {}).get("home", {}).get("name") or ""
            away_org = (f.get("teams") or {}).get("away", {}).get("name") or ""
            if not utc:
                continue
            aliases = TEAM_ALIASES.get(league, {})
            home = aliases.get(home_org)
            away = aliases.get(away_org)
            if not home or not away:
                skipped_team += 1
                print(f"[快速通道 {day} {league}] 警告：队名未映射，跳过 "
                      f"{home_org} vs {away_org}（请补充 team_aliases.py 的 {league} 表）")
                continue
            # fixture.date 是 UTC 时刻；football-data.co.uk CSV 记录的是联赛当地日期。
            # 按 LEAGUE_UTC_OFFSET 固定偏移换算（不追夏令时），深夜开球 ±1 天误差由
            # 「同队 ±2 天吸附」兜底：CSV 已有同对阵记录时直接采用 CSV 日期。
            dt = datetime.fromisoformat(utc.replace("Z", "+00:00")) + timedelta(
                hours=LEAGUE_UTC_OFFSET.get(league, 0))
            # 赛季字段沿用脚本现有惯例：跨年联赛记起始年，自然年联赛记比赛日历年
            season = start_year if league in LEAGUES_A else dt.date().year

            if status == "NS":  # 未开赛 → 近期赛程
                local_day = dt.date()
                if today <= local_day <= today + timedelta(days=FIXTURES_DAYS):
                    out_fixtures.append(make_fixture(
                        local_day.isoformat(), dt.strftime("%H:%M"),
                        home, away, season, league, team_cn))
                    added_fx += 1
                continue
            if status not in ("FT", "AET", "PEN"):
                continue
            ft = (f.get("score") or {}).get("fulltime") or {}
            s1, s2 = ft.get("home"), ft.get("away")
            if s1 is None or s2 is None:  # AET/PEN 但无常规时间比分则回退 goals
                goals = f.get("goals") or {}
                s1, s2 = goals.get("home"), goals.get("away")
            if s1 is None or s2 is None:
                continue
            date = dt.date().isoformat()
            for d in csv_index.get((league, home, away), ()):
                if abs((datetime.strptime(d, "%Y-%m-%d").date() - dt.date()).days) <= 2:
                    date = d
                    break
            res = "H" if s1 > s2 else ("A" if s1 < s2 else "D")
            out.append(make_match(date, home, s1, s2, away, res, season,
                                  league, team_cn, missing_teams))
            added += 1
        if added or added_fx or skipped_team:
            print(f"[快速通道 {day}] 本站联赛已赛 {added} 场、未赛赛程 {added_fx} 场入列"
                  f"（非本站联赛 {skipped_league} 场忽略，队名未映射跳过 {skipped_team} 场）")
        else:
            print(f"[快速通道 {day}] 本站联赛暂无相关比赛")
    return out, out_fixtures


def merge_fast(all_matches, fast_matches):
    """合并快速通道结果：去重键 (联赛, date, team1, team2)，同键时新源（快速通道）覆盖 CSV。
    保证 football_latest 里同一场比赛绝不出现两条。"""
    merged = {}
    for m in all_matches:
        merged[(m["_league"], m["date"], m["team1"], m["team2"])] = m
    overridden = 0
    for m in fast_matches:
        key = (m["_league"], m["date"], m["team1"], m["team2"])
        if key in merged:
            overridden += 1
        merged[key] = m
    if overridden:
        print(f"[快速通道] {overridden} 场与 CSV 重复，已用新源比分覆盖（未产生重复记录）")
    return list(merged.values())


def main():
    start_year = current_season_start()
    today = datetime.now().date()
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
    all_fixtures = []
    summary = {}
    fixtures_summary = {}

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
        if used_year == start_year:  # 回退到旧赛季的不会有未来赛程
            fx = parse_main_fixtures(text, league, used_year, team_cn, today)
            all_fixtures.extend(fx)
            fixtures_summary[league] = len(fx)

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

    # ---- 快速通道：API-Football 补最新赛果（无密钥时跳过，行为与旧版一致）----
    if APIFB_KEY:
        print(f"\n快速通道：API-Football 按天拉取最近 {FAST_DAYS} 天全球比赛"
              f"（每次间隔 {APIFB_SLEEP} 秒）...")
        csv_index = {}
        for m in all_matches:
            csv_index.setdefault((m["_league"], m["team1"], m["team2"]), set()).add(m["date"])
        before = len(all_matches)
        fast_matches, fast_fixtures = fetch_apifb_fast(csv_index, team_cn, missing_teams)
        all_matches = merge_fast(all_matches, fast_matches)
        all_fixtures.extend(fast_fixtures)
        summary["_fast"] = len(fast_matches)
        summary["_fast_fx"] = len(fast_fixtures)
        print(f"[快速通道] 赛果合计 {len(fast_matches)} 场，"
              f"净新增 {len(all_matches) - before} 场；赛程 {len(fast_fixtures)} 场\n")
    else:
        print("\n未设置 API_FOOTBALL_KEY，跳过快速通道（API-Football）")

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

    # ---- 近期赛程：按 (联赛, 日期, 时间, 主, 客) 去重后按开球时间排序 ----
    fx_merged = {}
    for fx in all_fixtures:
        fx_merged[(fx["_league"], fx["date"], fx["time"], fx["team1"], fx["team2"])] = fx
    all_fixtures = sorted(fx_merged.values(),
                          key=lambda x: (x["date"], x["time"] or "99:99", x["team1"]))
    fixtures_output = {
        "updated_at": output["updated_at"],
        "fixtures": all_fixtures,
    }
    with open(FIXTURES_PATH, "w", encoding="utf-8") as f:
        json.dump(fixtures_output, f, ensure_ascii=False, indent=2)
    with open(FIXTURES_JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.FIXTURES_DATA = ")
        json.dump(fixtures_output, f, ensure_ascii=False)
        f.write(";\n")

    print("\n===== 汇总 =====")
    for league in LEAGUES_A + LEAGUES_B:
        print(f"  {league}: {summary.get(league, 0)} 场")
    if "_fast" in summary:
        print(f"  快速通道(API-Football): {summary['_fast']} 场"
              + (f"（另获赛程 {summary['_fast_fx']} 场）" if summary.get("_fast_fx") else ""))
    print(f"  合计: {len(all_matches)} 场")
    print(f"  输出文件: {OUTPUT_PATH}")
    print(f"  输出文件: {OUTPUT_JS_PATH}")
    fx_total = len(all_fixtures)
    print(f"\n===== 近期赛程（未来 {FIXTURES_DAYS} 天） =====")
    for league in sorted(fixtures_summary):
        if fixtures_summary[league]:
            print(f"  {league}: {fixtures_summary[league]} 场")
    print(f"  合计: {fx_total} 场")
    print(f"  输出文件: {FIXTURES_PATH}")
    print(f"  输出文件: {FIXTURES_JS_PATH}")


if __name__ == "__main__":
    sys.exit(main())
