#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_history.py — 抓取 27 国 38 个联赛的历史赛果（2014 年起），输出：
  - leagues.js        window.LEAGUES_MANIFEST = [...]   （联赛清单，很小，启动加载）
  - data/{code}.js    window.LEAGUE_DATA = {...}        （每联赛一个文件，按需懒加载）

数据源 football-data.co.uk：
  A 类（22 个联赛码）：https://www.football-data.co.uk/mmz4281/{赛季码}/{联赛码}.csv
  B 类（16 个联赛码）：https://www.football-data.co.uk/new/{联赛码}.csv （单一全历史文件）

用法:
  python3 fetch_history.py                  # 抓取全部（有缓存则跳过网络）
  python3 fetch_history.py --leagues E0,SP1 # 只抓指定联赛
  python3 fetch_history.py --force          # 忽略缓存重新下载
  python3 fetch_history.py --rebuild        # 不访问网络，仅用缓存重建输出（改译名后用）
依赖: 仅标准库
"""

import argparse
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
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_DIR = os.path.join(BASE_DIR, ".cache")
TEAM_CN_PATH = os.path.join(BASE_DIR, "team_cn.json")
LEAGUES_JS_PATH = os.path.join(BASE_DIR, "leagues.js")

URL_A = "https://www.football-data.co.uk/mmz4281/{scode}/{code}.csv"
URL_B = "https://www.football-data.co.uk/new/{code}.csv"
TIMEOUT = 40
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) fetch_history/1.0"
FIRST_SEASON = 2014  # 2014-15 赛季起

FTR_MAP = {"H": "B", "A": "P", "D": "T"}

GROUP_MAIN = "欧洲·五大联赛"
GROUP_SECOND = "欧洲·次级联赛"
GROUP_EUROPE = "欧洲·其他联赛"
GROUP_AMERICAS = "美洲"
GROUP_ASIA = "亚洲"

# code, name, cn, country_cn, flag, group, kind("A"=mmz4281 / "B"=new 全历史文件)
LEAGUE_META = [
    ("E0",  "Premier League",        "英超",   "英格兰",   "🏴󠁧󠁢󠁥󠁮󠁧󠁿", GROUP_MAIN,    "A"),
    ("SP1", "La Liga",               "西甲",   "西班牙",   "🇪🇸", GROUP_MAIN,    "A"),
    ("D1",  "Bundesliga",            "德甲",   "德国",     "🇩🇪", GROUP_MAIN,    "A"),
    ("I1",  "Serie A",               "意甲",   "意大利",   "🇮🇹", GROUP_MAIN,    "A"),
    ("F1",  "Ligue 1",               "法甲",   "法国",     "🇫🇷", GROUP_MAIN,    "A"),
    ("E1",  "Championship",          "英冠",   "英格兰",   "🏴󠁧󠁢󠁥󠁮󠁧󠁿", GROUP_SECOND,  "A"),
    ("E2",  "League 1",              "英甲",   "英格兰",   "🏴󠁧󠁢󠁥󠁮󠁧󠁿", GROUP_SECOND,  "A"),
    ("E3",  "League 2",              "英乙",   "英格兰",   "🏴󠁧󠁢󠁥󠁮󠁧󠁿", GROUP_SECOND,  "A"),
    ("EC",  "National League",       "英议联", "英格兰",   "🏴󠁧󠁢󠁥󠁮󠁧󠁿", GROUP_SECOND,  "A"),
    ("SC1", "Scottish Championship", "苏冠",   "苏格兰",   "🏴󠁧󠁢󠁳󠁣󠁴󠁿", GROUP_SECOND,  "A"),
    ("SC2", "Scottish League 1",     "苏甲",   "苏格兰",   "🏴󠁧󠁢󠁳󠁣󠁴󠁿", GROUP_SECOND,  "A"),
    ("SC3", "Scottish League 2",     "苏乙",   "苏格兰",   "🏴󠁧󠁢󠁳󠁣󠁴󠁿", GROUP_SECOND,  "A"),
    ("D2",  "2. Bundesliga",         "德乙",   "德国",     "🇩🇪", GROUP_SECOND,  "A"),
    ("I2",  "Serie B",               "意乙",   "意大利",   "🇮🇹", GROUP_SECOND,  "A"),
    ("SP2", "La Liga 2",             "西乙",   "西班牙",   "🇪🇸", GROUP_SECOND,  "A"),
    ("F2",  "Ligue 2",               "法乙",   "法国",     "🇫🇷", GROUP_SECOND,  "A"),
    ("SC0", "Scottish Premiership",  "苏超",   "苏格兰",   "🏴󠁧󠁢󠁳󠁣󠁴󠁿", GROUP_EUROPE,  "A"),
    ("N1",  "Eredivisie",            "荷甲",   "荷兰",     "🇳🇱", GROUP_EUROPE,  "A"),
    ("B1",  "Pro League",            "比甲",   "比利时",   "🇧🇪", GROUP_EUROPE,  "A"),
    ("P1",  "Primeira Liga",         "葡超",   "葡萄牙",   "🇵🇹", GROUP_EUROPE,  "A"),
    ("T1",  "Süper Lig",             "土超",   "土耳其",   "🇹🇷", GROUP_EUROPE,  "A"),
    ("G1",  "Super League",          "希腊超", "希腊",     "🇬🇷", GROUP_EUROPE,  "A"),
    ("AUT", "Bundesliga",            "奥甲",   "奥地利",   "🇦🇹", GROUP_EUROPE,  "B"),
    ("DNK", "Superliga",             "丹超",   "丹麦",     "🇩🇰", GROUP_EUROPE,  "B"),
    ("FIN", "Veikkausliiga",         "芬超",   "芬兰",     "🇫🇮", GROUP_EUROPE,  "B"),
    ("IRL", "Premier Division",      "爱超",   "爱尔兰",   "🇮🇪", GROUP_EUROPE,  "B"),
    ("NOR", "Eliteserien",           "挪超",   "挪威",     "🇳🇴", GROUP_EUROPE,  "B"),
    ("POL", "Ekstraklasa",           "波甲",   "波兰",     "🇵🇱", GROUP_EUROPE,  "B"),
    ("ROU", "Liga 1",                "罗甲",   "罗马尼亚", "🇷🇴", GROUP_EUROPE,  "B"),
    ("RUS", "Premier League",        "俄超",   "俄罗斯",   "🇷🇺", GROUP_EUROPE,  "B"),
    ("SWE", "Allsvenskan",           "瑞典超", "瑞典",     "🇸🇪", GROUP_EUROPE,  "B"),
    ("SWZ", "Super League",          "瑞士超", "瑞士",     "🇨🇭", GROUP_EUROPE,  "B"),
    ("ARG", "Liga Profesional",      "阿甲",   "阿根廷",   "🇦🇷", GROUP_AMERICAS, "B"),
    ("BRA", "Serie A",               "巴甲",   "巴西",     "🇧🇷", GROUP_AMERICAS, "B"),
    ("MEX", "Liga MX",               "墨超",   "墨西哥",   "🇲🇽", GROUP_AMERICAS, "B"),
    ("USA", "MLS",                   "美职联", "美国",     "🇺🇸", GROUP_AMERICAS, "B"),
    ("CHN", "Super League",          "中超",   "中国",     "🇨🇳", GROUP_ASIA,    "B"),
    ("JPN", "J1 League",             "J联赛",  "日本",     "🇯🇵", GROUP_ASIA,    "B"),
]

# B 类代码 404 时的备选代码
B_ALT_CODES = {"SWZ": "SUI"}

META_BY_CODE = {m[0]: m for m in LEAGUE_META}


def current_season_start(today=None):
    """当前赛季起始年：月份 >= 7 则为当年，否则为上年。"""
    today = today or datetime.now()
    return today.year if today.month >= 7 else today.year - 1


def season_code(start_year):
    """2025 -> '2526'"""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def parse_date(s):
    """dd/mm/yy 或 dd/mm/yyyy -> YYYY-MM-DD；失败返回 None。"""
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def fetch_url(url, cache_name, sleep_s, force=False, rebuild=False):
    """带缓存与限速的下载。返回文本；404 返回 None；其他异常抛出。"""
    path = os.path.join(CACHE_DIR, cache_name)
    mark = path + ".404"
    if not force:
        if os.path.exists(mark):
            return None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    if rebuild:
        return None
    time.sleep(sleep_s)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            open(mark, "w").close()
            return None
        raise
    text = raw.decode("utf-8-sig", errors="replace")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if os.path.exists(mark):
        os.remove(mark)
    return text


def parse_main_csv(text):
    """A 类 CSV（HomeTeam/AwayTeam/FTHG/FTAG/FTR），返回原始行列表。"""
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        home = (row.get("HomeTeam") or "").strip()
        away = (row.get("AwayTeam") or "").strip()
        ftr = (row.get("FTR") or "").strip().upper()
        fthg = (row.get("FTHG") or "").strip()
        ftag = (row.get("FTAG") or "").strip()
        date = parse_date(row.get("Date"))
        if not home or not away or not date or ftr not in FTR_MAP:
            continue
        try:
            s1, s2 = int(fthg), int(ftag)
        except ValueError:
            continue
        rows.append({"date": date, "team1": home, "s1": s1, "s2": s2,
                     "team2": away, "result": FTR_MAP[ftr]})
    return rows


def parse_extra_csv(text):
    """B 类 CSV（Country/League/Season/Date/Home/Away/HG/AG/Res）。
    返回 (行列表, season_type)。行带 season 起始年（int）。"""
    rows = []
    type_votes = {"split": 0, "calendar": 0}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        season_raw = (row.get("Season") or "").strip()
        home = (row.get("Home") or "").strip()
        away = (row.get("Away") or "").strip()
        res = (row.get("Res") or "").strip().upper()
        hg = (row.get("HG") or "").strip()
        ag = (row.get("AG") or "").strip()
        date = parse_date(row.get("Date"))
        if not season_raw or not home or not away or not date or res not in FTR_MAP:
            continue
        try:
            s1, s2 = int(hg), int(ag)
        except ValueError:
            continue
        # 赛季："2014/2015"（跨年）或 "2014"（自然年）
        if "/" in season_raw:
            type_votes["split"] += 1
        else:
            type_votes["calendar"] += 1
        try:
            start = int(season_raw[:4])
        except ValueError:
            continue
        if start < FIRST_SEASON:
            continue
        rows.append({"date": date, "team1": home, "s1": s1, "s2": s2,
                     "team2": away, "result": FTR_MAP[res], "_season": start})
    season_type = "split" if type_votes["split"] >= type_votes["calendar"] else "calendar"
    return rows, season_type


def build_matches(rows, code, season_start, team_cn, missing):
    """原始行 -> 网站 match 对象，补充中文名与 season 字段。"""
    out = []
    for r in rows:
        for team in (r["team1"], r["team2"]):
            if team not in team_cn:
                team_cn[team] = team
                missing.add(team)
        out.append({
            "date": r["date"],
            "team1": r["team1"],
            "s1": r["s1"],
            "s2": r["s2"],
            "team2": r["team2"],
            "result": r["result"],
            "team1_cn": team_cn.get(r["team1"], r["team1"]),
            "team2_cn": team_cn.get(r["team2"], r["team2"]),
            "season": str(r.get("_season", season_start)),
            "_league": code,
        })
    return out


def fetch_league(code, args, team_cn, missing):
    """抓取单个联赛全部历史，返回 (matches, season_type)。"""
    meta = META_BY_CODE[code]
    kind = meta[6]
    all_rows = []
    season_type = "split"

    if kind == "A":
        cur = current_season_start()
        for year in range(FIRST_SEASON, cur + 1):
            scode = season_code(year)
            url = URL_A.format(scode=scode, code=code)
            try:
                text = fetch_url(url, f"{code}_{scode}.csv", args.sleep,
                                 force=args.force, rebuild=args.rebuild)
            except Exception as e:
                print(f"  [{code}] {scode} 下载失败：{e}，跳过")
                continue
            if text is None:
                continue  # 404（如尚未开始的新赛季）
            rows = parse_main_csv(text)
            all_rows.extend((dict(r, _season=year) for r in rows))
            print(f"  [{code}] {scode}: {len(rows)} 场")
    else:
        codes = [code] + ([B_ALT_CODES[code]] if code in B_ALT_CODES else [])
        text = None
        for c in codes:
            try:
                text = fetch_url(URL_B.format(code=c), f"new_{c}.csv", args.sleep,
                                 force=args.force, rebuild=args.rebuild)
            except Exception as e:
                print(f"  [{code}] 全历史文件下载失败：{e}")
                text = None
            if text is not None:
                if c != code:
                    print(f"  [{code}] 主代码 404，已改用备选代码 {c}")
                break
        if text is None:
            print(f"  [{code}] 警告：无数据（404 或缓存缺失）")
            return [], season_type
        all_rows, season_type = parse_extra_csv(text)
        print(f"  [{code}] 全历史（{FIRST_SEASON} 起）: {len(all_rows)} 场，赛季类型 {season_type}")

    # 按赛季分组构建 match 对象
    matches = []
    for r in all_rows:
        season_start = r.pop("_season")
        matches.extend(build_matches([r], code, season_start, team_cn, missing))

    # 去重 + 排序
    seen = set()
    deduped = []
    for m in sorted(matches, key=lambda x: x["date"]):
        key = (m["date"], m["team1"], m["team2"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)
    return deduped, season_type


def write_league_data(code, matches, season_type):
    meta = META_BY_CODE[code]
    obj = {"code": code, "name": meta[1], "cn": meta[2],
           "season_type": season_type, "matches": matches}
    path = os.path.join(DATA_DIR, f"{code}.js")
    with open(path, "w", encoding="utf-8") as f:
        f.write("window.LEAGUE_DATA = ")
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    return path


def read_existing_counts():
    """读取已生成的 data/*.js，返回 {code: (count, season_type)}。"""
    counts = {}
    if not os.path.isdir(DATA_DIR):
        return counts
    for fn in os.listdir(DATA_DIR):
        if not fn.endswith(".js"):
            continue
        code = fn[:-3]
        try:
            with open(os.path.join(DATA_DIR, fn), encoding="utf-8") as f:
                text = f.read()
            body = text.strip()
            body = body[len("window.LEAGUE_DATA = "):].rstrip(";")
            obj = json.loads(body)
            counts[code] = (len(obj.get("matches", [])),
                            obj.get("season_type", "split"))
        except Exception:
            continue
    return counts


def write_manifest():
    counts = read_existing_counts()
    manifest = []
    for code, name, cn, country_cn, flag, group, kind in LEAGUE_META:
        cnt, stype = counts.get(code, (0, "split" if kind == "A" else "calendar"))
        manifest.append({
            "code": code, "name": name, "cn": cn, "country_cn": country_cn,
            "flag": flag, "group": group, "season_type": stype, "matches": cnt,
        })
    with open(LEAGUES_JS_PATH, "w", encoding="utf-8") as f:
        f.write("window.LEAGUES_MANIFEST = ")
        json.dump(manifest, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", default="",
                    help="只抓指定联赛码，逗号分隔（默认全部）")
    ap.add_argument("--force", action="store_true", help="忽略缓存重新下载")
    ap.add_argument("--rebuild", action="store_true",
                    help="不访问网络，仅用缓存重建输出文件")
    ap.add_argument("--sleep", type=float, default=1.2, help="请求间隔秒数")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    if args.leagues:
        wanted = [c.strip().upper() for c in args.leagues.split(",") if c.strip()]
        codes = [c for c in (m[0] for m in LEAGUE_META) if c in wanted]
        unknown = set(wanted) - set(codes)
        if unknown:
            print(f"警告：未知联赛码 {sorted(unknown)}")
    else:
        codes = [m[0] for m in LEAGUE_META]

    # 加载中文名映射
    if os.path.exists(TEAM_CN_PATH):
        with open(TEAM_CN_PATH, encoding="utf-8") as f:
            team_cn = json.load(f)
    else:
        team_cn = {}

    missing = set()
    total = 0
    for code in codes:
        print(f"[{code}] 开始抓取...")
        matches, season_type = fetch_league(code, args, team_cn, missing)
        if not matches:
            print(f"[{code}] 无数据，跳过写入")
            continue
        path = write_league_data(code, matches, season_type)
        total += len(matches)
        size_kb = os.path.getsize(path) // 1024
        print(f"[{code}] 完成：{len(matches)} 场 -> {os.path.relpath(path, BASE_DIR)}（{size_kb} KB）")

    manifest = write_manifest()
    avail = sum(1 for m in manifest if m["matches"] > 0)
    print(f"\nleagues.js 已写入：{avail}/{len(manifest)} 个联赛有数据")

    if missing:
        with open(TEAM_CN_PATH, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(team_cn.items())), f, ensure_ascii=False, indent=2)
        print(f"\n以下 {len(missing)} 支球队缺少中文名（已按英文名写入 team_cn.json）：")
        for t in sorted(missing):
            print(f"  - {t}")

    print(f"\n本次运行合计 {total} 场")


if __name__ == "__main__":
    sys.exit(main())
