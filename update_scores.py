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
OU_PATH = os.path.join(BASE_DIR, "football_ou.json")
OU_JS_PATH = os.path.join(BASE_DIR, "football_ou.js")
OU_STORE_PATH = os.path.join(BASE_DIR, "odds_store.json")  # 初盘快照持久化（首次抓到即存，不覆盖）
OU_MAX_CALLS = 45      # 每次运行 odds 调用上限（fixtures 11 次之外，总预算 ≤60 次/运行）
OU_STORE_DAYS = 30     # 快照保留天数（按比赛日期裁剪，防文件无限增长）
OU_DAYS = 14           # football_ou 输出近 14 天完场对照

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
FAST_DAYS = 4      # 拉取 最近3天+今天（Pro 7500 次/天，余量充足；免费版只能填 2）
FIXTURE_DAYS = 7   # 赛程向前拉取天数（今天+未来7天；免费版窗口只到明天，填 1）
APIFB_SLEEP = 6    # 调用间隔秒数（限流保护）
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


def make_fixture(date, time_s, home, away, season, league, team_cn, fid=None):
    """未来赛程记录；新球队同样先以英文名占位（通常已赛球队已在 team_cn 中）。
    fid 为 API-Football fixture id（仅快速通道来源有），用于 /odds?fixture= 抓初盘。"""
    for team in (home, away):
        if team not in team_cn:
            team_cn[team] = team
    rec = {
        "date": date,
        "time": time_s,
        "team1": home,
        "team2": away,
        "team1_cn": team_cn.get(home, home),
        "team2_cn": team_cn.get(away, away),
        "season": str(season),
        "_league": league,
    }
    if fid is not None:
        rec["fid"] = fid
    return rec


def parse_main_fixtures(text, league, season_start, team_cn, today):
    """A 类 CSV 中比分为空（FTHG 无值）的已排期行 = 未来赛程。
    只保留 [今天, 今天+FIXTURE_DAYS] 窗口内的场次。B 类 /new/*.csv 只含已赛场次，
    经核实文件末尾即为最新已赛日期、无未来行，故未来赛程仅取 A 类。"""
    last_day = today + timedelta(days=FIXTURE_DAYS)
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
    赛程收 NS（未开赛）：除赛果窗口外再向前拉 FIXTURE_DAYS 天（今天+未来7天），
    得出未来一周赛程；CSV 一旦出现排期行会与之按同键去重合并。
    """
    today = datetime.now(timezone.utc).date()
    dates = [(today - timedelta(days=FAST_DAYS - 1 - i)) for i in range(FAST_DAYS)]
    dates += [today + timedelta(days=i) for i in range(1, FIXTURE_DAYS + 1)]  # 未来赛程窗口
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
                if today <= local_day <= today + timedelta(days=FIXTURE_DAYS):
                    out_fixtures.append(make_fixture(
                        local_day.isoformat(), dt.strftime("%H:%M"),
                        home, away, season, league, team_cn,
                        fid=(f.get("fixture") or {}).get("id")))
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


# ---- 初盘大小球：/odds 快照 ----
# API-Football 不区分初盘/临场，「初盘」= 我们首次看到该场 Goals Over/Under 盘口时的快照。
# fixtures 提前 7 天进入窗口，首次抓到即存 odds_store.json，之后绝不覆盖。
APIFB_ODDS_URL = "https://v3.football.api-sports.io/odds?fixture={fid}"


def ou_key_tuple(league, date, team1, team2):
    return f"{league}|{date}|{team1}|{team2}"


def load_ou_store():
    if os.path.exists(OU_STORE_PATH):
        try:
            with open(OU_STORE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            print("警告：odds_store.json 损坏，从空快照重新开始")
    return {}


def save_ou_store(store):
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=OU_STORE_DAYS)).isoformat()
    store = {k: v for k, v in store.items() if v.get("date", "9999") >= cutoff}
    with open(OU_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2, sort_keys=True)
    return store


def pick_ou_market(bookmakers):
    """从 bookmakers 列表选大小球主线。
    市场优先级：'Goal Line'（亚洲大小球，含 2.25/2.75 等四分之一盘）> 'Goals Over/Under'；
    庄家优先级：Bet365 > 第一家有对应市场的。
    主线 = 所选市场内两边赔率最接近的盘口（即庄家眼中的平衡盘）。
    返回 {'line', 'over', 'under', 'bm', 'market'} 或 None。"""
    def extract(bm, market_name):
        lines = {}
        for bet in bm.get("bets", []):
            if bet.get("name") != market_name:
                continue
            for v in bet.get("values", []):
                parts = (v.get("value") or "").split()
                if len(parts) != 2:
                    continue
                side, line_s = parts
                try:
                    lf = float(line_s)
                    odd = float(v.get("odd"))
                except (TypeError, ValueError):
                    continue
                lines.setdefault(lf, {})[side.lower()] = odd
        cands = [(abs(o["over"] - o["under"]), lf, o)
                 for lf, o in lines.items() if "over" in o and "under" in o]
        if not cands:
            return None
        cands.sort(key=lambda x: x[0])
        _, lf, o = cands[0]
        return {"line": lf, "over": o["over"], "under": o["under"]}

    fallback_gl = fallback_gou = None
    for bm in bookmakers:
        name = (bm.get("name") or "").strip().lower()
        for market in ("Goal Line", "Goals Over/Under"):
            cand = extract(bm, market)
            if not cand:
                continue
            cand["bm"] = bm.get("name") or "?"
            cand["market"] = market
            if name == "bet365":
                return cand
            if market == "Goal Line" and fallback_gl is None:
                fallback_gl = cand
            elif market == "Goals Over/Under" and fallback_gou is None:
                fallback_gou = cand
    return fallback_gl or fallback_gou


def fetch_ou_snapshots(fixtures, store):
    """对窗口内无快照的未赛场次抓初盘（每场 1 次 /odds?fixture= 调用，每轮上限 OU_MAX_CALLS，
    临近开赛优先）。快照一旦写入绝不覆盖；盘口未开出留待下轮；已过开赛日仍无盘口则封盘放弃。
    返回 (本轮新抓, 仍缺)。"""
    today = datetime.now(timezone.utc).date()
    pending = []
    for fx in fixtures:
        key = ou_key_tuple(fx["_league"], fx["date"], fx["team1"], fx["team2"])
        old = store.get(key)
        if old and (old.get("line") is not None or old.get("closed")):
            continue  # 已有初盘或已封盘
        if not fx.get("fid"):
            continue  # CSV 来源无 fixture id，无法抓 odds
        pending.append((key, fx))
    pending.sort(key=lambda x: (x[1]["date"], x[1].get("time") or "99:99"))

    got = still = 0
    for key, fx in pending[:OU_MAX_CALLS]:
        time.sleep(1.0)  # Pro 限流宽松，1 秒足够
        req = urllib.request.Request(
            APIFB_ODDS_URL.format(fid=fx["fid"]),
            headers={"x-apisports-key": APIFB_KEY, "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.load(resp)
        except Exception as e:
            print(f"[初盘 {fx['_league']}] {fx['team1']} vs {fx['team2']} 请求失败：{e}")
            still += 1
            continue
        if data.get("errors"):
            print(f"[初盘 {fx['_league']}] API 返回错误 {data['errors']}")
            still += 1
            continue
        bms = (data.get("response") or [{}])[0].get("bookmakers") or []
        snap = pick_ou_market(bms)
        if snap:
            snap.update({
                "fid": fx["fid"],
                "first_seen": datetime.now().astimezone().isoformat(timespec="seconds"),
                "league": fx["_league"], "date": fx["date"],
                "time": fx.get("time") or "",
                "team1": fx["team1"], "team2": fx["team2"],
            })
            store[key] = snap  # 仅在无快照时到达这里，天然不覆盖
            got += 1
        else:
            d = datetime.strptime(fx["date"], "%Y-%m-%d").date()
            if d < today:  # 已过开赛日仍无盘口（小联赛不开盘），封盘避免反复抓
                store[key] = {"line": None, "closed": True, "fid": fx["fid"],
                              "league": fx["_league"], "date": fx["date"],
                              "time": fx.get("time") or "",
                              "team1": fx["team1"], "team2": fx["team2"]}
            still += 1
    overflow = len(pending) - min(len(pending), OU_MAX_CALLS)
    if overflow > 0:
        print(f"[初盘] 本轮达调用上限，{overflow} 场留待下轮")
    return got, still + overflow


def safe_write_output(json_path, js_path, js_var, payload, list_key, label):
    """防空写保护：新列表为空、或不足旧文件一半（旧文件 ≥10 条）时，判定本次抓取异常，
    保留磁盘旧文件不覆盖并打印警告。旧文件缺失/损坏时按无旧数据处理，正常写入。
    返回 True=已写入，False=已保留旧文件。"""
    new_count = len(payload.get(list_key) or [])
    old_count = 0
    if os.path.exists(json_path):
        try:
            with open(json_path, encoding="utf-8") as f:
                old_count = len((json.load(f) or {}).get(list_key) or [])
        except (ValueError, OSError):
            old_count = 0
    if old_count and (new_count == 0 or new_count < old_count * 0.5):
        print(f"[防空写] 警告：{label} 本次仅 {new_count} 条，旧文件有 {old_count} 条，"
              f"疑似抓取失败，已保留旧文件不覆盖（{os.path.basename(json_path)} 及其 .js）")
        return False
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(js_path, "w", encoding="utf-8") as f:
        f.write(f"window.{js_var} = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";\n")
    return True


def attach_beijing_time(fx):
    """给赛程记录补 date_bj / time_bj（北京时间，UTC+8）：
    联赛当地时间 + (8 - LEAGUE_UTC_OFFSET[联赛]) 小时，跨天自动进位。
    固定偏移不追夏令时，±1 小时误差可接受；找不到偏移按 UTC+0 处理。
    time 为空（CSV 缺开球时间）时 time_bj 同样留空、date_bj 按当天 12:00 折算（不跨天）。"""
    off = LEAGUE_UTC_OFFSET.get(fx.get("_league"), 0)
    time_s = (fx.get("time") or "").strip()
    hh, mm = 12, 0  # 无开球时间按正午算，日期不因换算漂移
    if time_s:
        try:
            hh, mm = int(time_s[:2]), int(time_s[3:5])
        except (ValueError, IndexError):
            pass
    try:
        dt = datetime.strptime(fx["date"], "%Y-%m-%d").replace(hour=hh, minute=mm)
    except (KeyError, ValueError):
        return fx
    dt += timedelta(hours=8 - off)
    fx["date_bj"] = dt.date().isoformat()
    fx["time_bj"] = dt.strftime("%H:%M") if time_s else ""
    return fx


def settle_ou(total, line):
    """总进球 vs 初盘线 → 大/小/走/半大/半小。
    半球盘（x.5）：无走水；整盘（x.0）：进球数等于线=走水退本金；
    四分之一盘（x.25/x.75）：拆成相邻两半各结算一半（如 2.75 = 2.5 半 + 3.0 半），
    总进球为整数时两半结果只会是 半大(赢半) 或 半小(输半)，不会出现整走。"""
    if line is None:
        return None
    q = int(round(line * 4))
    if q % 2 == 0:
        l = q / 4
        return "大" if total > l else ("小" if total < l else "走")
    pts = 0
    for h in ((q - 1) / 4, (q + 1) / 4):
        pts += 1 if total > h else (-1 if total < h else 0)
    return {2: "大", 1: "半大", -1: "半小", -2: "小"}.get(pts, "走")


def annotate_ou(records, store, with_result):
    """给赛果/赛程记录回写 ou_line/ou_over/ou_under/ou_bm（及赛果的 ou_result）。"""
    n = 0
    for r in records:
        snap = store.get(ou_key_tuple(r["_league"], r["date"], r["team1"], r["team2"]))
        if not snap or snap.get("line") is None:
            continue
        r["ou_line"] = snap["line"]
        r["ou_over"] = snap.get("over")
        r["ou_under"] = snap.get("under")
        r["ou_bm"] = snap.get("bm")
        if with_result and r.get("s1") is not None and r.get("s2") is not None:
            r["ou_result"] = settle_ou(r["s1"] + r["s2"], snap["line"])
        n += 1
    return n


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

    # ---- 初盘大小球：抓快照（首次抓到即初盘，不覆盖）并回写到赛果/赛程 ----
    ou_store = load_ou_store()
    if APIFB_KEY:
        got, lack = fetch_ou_snapshots(all_fixtures, ou_store)
        print(f"[初盘] 本轮新抓 {got} 场初盘快照，{lack} 场盘口未开出（下轮再试）")
    ou_store = save_ou_store(ou_store)  # 裁剪过期快照
    fx_ou = annotate_ou(all_fixtures, ou_store, with_result=False)
    m_ou = annotate_ou(all_matches, ou_store, with_result=True)
    print(f"[初盘] 赛程回写 {fx_ou} 场初盘，赛果回写 {m_ou} 场大小球对照")

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
    latest_written = safe_write_output(OUTPUT_PATH, OUTPUT_JS_PATH,
                                       "LATEST_DATA", output, "matches", "赛果增量")

    # ---- 近期赛程：按 (联赛, 日期, 时间, 主, 客) 去重后按开球时间排序 ----
    # 保留 fid（API-Football fixture id）：fetch_predictions.py 等下游脚本需要它调 /predictions
    fx_merged = {}
    for fx in all_fixtures:
        fx_merged[(fx["_league"], fx["date"], fx["time"], fx["team1"], fx["team2"])] = fx
    all_fixtures = sorted(fx_merged.values(),
                          key=lambda x: (x["date"], x["time"] or "99:99", x["team1"]))
    for fx in all_fixtures:
        attach_beijing_time(fx)  # 补 date_bj / time_bj 北京时间字段（显示层用，原 date/time 不动）
    fixtures_output = {
        "updated_at": output["updated_at"],
        "fixtures": all_fixtures,
    }
    fx_written = safe_write_output(FIXTURES_PATH, FIXTURES_JS_PATH,
                                   "FIXTURES_DATA", fixtures_output, "fixtures", "近期赛程")

    # ---- 大小球对照输出：近 OU_DAYS 天完场（含结果）+ 窗口内未赛（初盘）----
    ou_cutoff = (today - timedelta(days=OU_DAYS)).isoformat()
    ou_items = []
    for m in all_matches:
        if m.get("ou_line") is not None and m["date"] >= ou_cutoff:
            ou_items.append({
                "date": m["date"], "time": "", "_league": m["_league"],
                "team1": m["team1"], "team2": m["team2"],
                "team1_cn": m["team1_cn"], "team2_cn": m["team2_cn"],
                "status": "FT", "s1": m["s1"], "s2": m["s2"],
                "ou_line": m["ou_line"], "ou_bm": m.get("ou_bm"),
                "ou_result": m.get("ou_result"),
            })
    for fx in all_fixtures:
        if fx.get("ou_line") is not None:
            ou_items.append({
                "date": fx["date"], "time": fx.get("time") or "",
                "_league": fx["_league"],
                "team1": fx["team1"], "team2": fx["team2"],
                "team1_cn": fx["team1_cn"], "team2_cn": fx["team2_cn"],
                "status": "NS",
                "ou_line": fx["ou_line"], "ou_bm": fx.get("ou_bm"),
                "ou_over": fx.get("ou_over"), "ou_under": fx.get("ou_under"),
            })
    ou_items.sort(key=lambda x: (x["date"], x["time"] or "99:99", x["team1"]))
    ou_output = {"updated_at": output["updated_at"], "items": ou_items}
    ou_written = safe_write_output(OU_PATH, OU_JS_PATH,
                                   "OU_DATA", ou_output, "items", "大小球对照")

    print("\n===== 汇总 =====")
    for league in LEAGUES_A + LEAGUES_B:
        print(f"  {league}: {summary.get(league, 0)} 场")
    if "_fast" in summary:
        print(f"  快速通道(API-Football): {summary['_fast']} 场"
              + (f"（另获赛程 {summary['_fast_fx']} 场）" if summary.get("_fast_fx") else ""))
    print(f"  合计: {len(all_matches)} 场")
    print(f"  输出文件: {OUTPUT_PATH}{'' if latest_written else '（防空写：保留旧文件）'}")
    print(f"  输出文件: {OUTPUT_JS_PATH}{'' if latest_written else '（防空写：保留旧文件）'}")
    fx_total = len(all_fixtures)
    print(f"\n===== 近期赛程（未来 {FIXTURE_DAYS} 天） =====")
    for league in sorted(fixtures_summary):
        if fixtures_summary[league]:
            print(f"  {league}: {fixtures_summary[league]} 场")
    print(f"  合计: {fx_total} 场")
    print(f"  输出文件: {FIXTURES_PATH}{'' if fx_written else '（防空写：保留旧文件）'}")
    print(f"  输出文件: {FIXTURES_JS_PATH}{'' if fx_written else '（防空写：保留旧文件）'}")
    ou_ft = sum(1 for i in ou_items if i["status"] == "FT")
    ou_ns = len(ou_items) - ou_ft
    print(f"\n===== 大小球对照 =====")
    print(f"  完场对照 {ou_ft} 场，未赛初盘 {ou_ns} 场")
    print(f"  输出文件: {OU_PATH}{'' if ou_written else '（防空写：保留旧文件）'}")
    print(f"  输出文件: {OU_JS_PATH}{'' if ou_written else '（防空写：保留旧文件）'}")


if __name__ == "__main__":
    sys.exit(main())
