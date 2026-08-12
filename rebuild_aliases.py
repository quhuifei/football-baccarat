#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rebuild_aliases.py — 一次性重建 team_aliases.py（不进主流程，不要每次跑）。

Pro 版 API-Football 调 GET /teams?league={id}&season=2026（空则回退 2025）
拿 38 个联赛当前赛季官方队名；football-data.co.uk 短名池来自：
  1. 该联赛当前/上一赛季 CSV 实际出现的 HomeTeam/AwayTeam（最权威）
  2. team_cn.json 全历史键（兜底，覆盖升班马等 CSV 还没出现的队）

匹配优先级：现有 TEAM_ALIASES 条目 > 归一化完全相等 > 词集合包含 > difflib 模糊。
结果写入 team_aliases_new.py；未能匹配的列入 alias_report.txt 待人工补
（人工补充填在本文件 MANUAL 里再重跑即可）。

用法: API_FOOTBALL_KEY=xxx python3 rebuild_aliases.py
"""

import csv
import difflib
import io
import json
import os
import re
import time
import unicodedata
import urllib.request

import update_scores as us
from team_aliases import TEAM_ALIASES

KEY = us.APIFB_KEY
if not KEY:
    raise SystemExit("请设置 API_FOOTBALL_KEY")

REPORT_PATH = os.path.join(us.BASE_DIR, "alias_report.txt")
NEW_PATH = os.path.join(us.BASE_DIR, "team_aliases_new.py")

# 人工补丁：{联赛码: {API 官方队名: football-data 短名}}，对不上的填这里再重跑
# 注：标「未证实」的是升班马，football-data 当前 CSV 尚未出现该队，按命名惯例推断，
#     等官方 CSV 出现后可核对修正（错了的后果只是不与 CSV 去重，不会污染别队数据）
MANUAL = {
    "P1": {"Sporting CP": "Sp Lisbon"},          # football-data 用 Sp Lisbon（非 Sporting Kansas City）
    "ARG": {"Gimnasia M.": "Gimnasia Mendoza"},  # 门多萨体操（非拉普拉塔体操 Gimnasia L.P.）
    "FIN": {"Turku PS": "TPS"},                  # 图尔库 TPS（非 KuPS）
    "CHN": {"Shenyang Urban": "Liaoning Tieren",  # 沈阳城市已更名辽宁铁人
            "Qingdao Youth Island": "Qingdao West Coast"},  # 青岛西海岸旧称
    "ROU": {"Universitatea Cluj": "U. Cluj",
            "Rapid": "FC Rapid Bucuresti"},        # 布加勒斯特快速（非奥甲 SK Rapid）
    "RUS": {"Dynamo": "Dynamo Moscow"},            # 莫斯科迪纳摩（非美职 Houston Dynamo）
    "SP2": {"Real Sociedad II": "Sociedad B", "Celta de Vigo II": "Celta B"},  # B队；Celta B 未证实
    "I2": {"Arezzo": "Arezzo"},                  # 升班马，未证实
    "T1": {"Çorum FK": "Corum", "Amed": "Amed"},  # 升班马，未证实
    "G1": {"Kalamata": "Kalamata", "OFI": "OFI Crete"},  # Kalamata 升班马，未证实
    "JPN": {"JEF United Chiba": "Chiba", "Mito Hollyhock": "Mito"},  # 升班马，未证实
}

CODE_BY_API_ID = {v: k for k, v in us.APIFB_LEAGUES.items()}  # 注意：下面是 id->code，需要反转
CODE_BY_API_ID = {api_id: code for api_id, code in us.APIFB_LEAGUES.items()}
API_ID_BY_CODE = {code: api_id for api_id, code in us.APIFB_LEAGUES.items()}

NOISE = {"fc", "afc", "cf", "sc", "ac", "as", "ss", "rcd", "ud", "cd", "sk",
         "bk", "if", "fk", "ff", "vfl", "vfb", "tsv", "sv", "rc", "rsc",
         "club", "de", "the", "1", "1893", "1907", "93"}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ü", "u").replace("ö", "o").replace("ä", "a")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    words = [w for w in s.split() if w not in NOISE]
    return " ".join(words)


def api_get(path):
    req = urllib.request.Request(
        "https://v3.football.api-sports.io/" + path,
        headers={"x-apisports-key": KEY, "User-Agent": us.USER_AGENT})
    with urllib.request.urlopen(req, timeout=us.TIMEOUT) as resp:
        return json.load(resp)


def fetch_api_teams(code):
    """当前赛季官方队名；2026 为空回退 2025。"""
    api_id = API_ID_BY_CODE[code]
    for season in (2026, 2025):
        time.sleep(1.0)
        data = api_get(f"teams?league={api_id}&season={season}")
        if data.get("errors"):
            print(f"[{code}] /teams season={season} 错误: {data['errors']}")
            continue
        names = [t["team"]["name"] for t in data.get("response", [])]
        if names:
            return names, season
    return [], None


def fetch_csv_names(code):
    """该联赛 football-data 短名池（当前/上一赛季 CSV）。"""
    names = set()
    start_year = us.current_season_start()
    if code in us.LEAGUES_A:
        for year in (start_year, start_year - 1):
            url = us.URL_TEMPLATE.format(code=us.season_code(year), league=code)
            try:
                text = us.download(url)
            except Exception:
                text = None
            if text is None:
                continue
            for row in csv.DictReader(io.StringIO(text)):
                for col in ("HomeTeam", "AwayTeam"):
                    v = (row.get(col) or "").strip()
                    if v:
                        names.add(v)
            if names and year == start_year:
                break  # 当前赛季已拿到就不再回退
    else:
        codes = [code] + ([us.B_ALT_CODES[code]] if code in us.B_ALT_CODES else [])
        for c in codes:
            try:
                text = us.download(us.URL_EXTRA.format(league=c))
            except Exception:
                text = None
            if text is None:
                continue
            for row in csv.DictReader(io.StringIO(text)):
                for col in ("Home", "Away"):
                    v = (row.get(col) or "").strip()
                    if v:
                        names.add(v)
            break
    return names


def match_one(api_name, pool_norm, pool_names, cutoff=0.62):
    """返回 (csv_name or None, 匹配方式)。"""
    n = norm(api_name)
    if not n:
        return None, "empty"
    if n in pool_norm:
        return pool_norm[n], "exact"
    # 词集合包含（如 'nurnberg' ⊂ '1 nurnberg' 已在归一化去掉 1/fc；这里处理 'dynamo dresden' vs 'dresden'）
    nw = set(n.split())
    cands = []
    for pn, orig in pool_norm.items():
        pw = set(pn.split())
        if nw and pw and (nw <= pw or pw <= nw):
            cands.append((len(nw & pw) / max(len(nw | pw), 1), orig))
    if cands:
        cands.sort(reverse=True)
        return cands[0][1], "subset"
    close = difflib.get_close_matches(n, list(pool_norm), n=1, cutoff=cutoff)
    if close:
        return pool_norm[close[0]], "fuzzy"
    return None, "miss"


def main():
    team_cn = json.load(open(us.TEAM_CN_PATH, encoding="utf-8"))
    global_pool = sorted(team_cn.keys())
    new_aliases = {}
    report = []
    total = covered = 0

    for code in us.LEAGUES_A + us.LEAGUES_B:
        api_names, season = fetch_api_teams(code)
        csv_names = fetch_csv_names(code)
        # 匹配池：本联赛 CSV 名 + 全历史池（归一化后本联赛优先）
        pool_names = sorted(csv_names) + [n for n in global_pool if n not in csv_names]
        pool_norm = {}
        for name in pool_names:
            pool_norm.setdefault(norm(name), name)

        existing = TEAM_ALIASES.get(code, {})
        manual = MANUAL.get(code, {})
        league_map = {}
        misses = []
        fuzzy_hits = []
        for name in api_names:
            total += 1
            if name in manual:  # 人工补丁优先于旧表（旧表可能有错配）
                league_map[name] = manual[name]
                covered += 1
                continue
            if name in existing:
                league_map[name] = existing[name]
                covered += 1
                continue
            csv_name, how = match_one(name, pool_norm, pool_names)
            if csv_name:
                league_map[name] = csv_name
                covered += 1
                if how == "fuzzy":
                    fuzzy_hits.append((name, csv_name))
            else:
                misses.append(name)

        # 保留现有表里、当前赛季 API 名单之外的旧条目（赛季中换名/历史兼容用）
        for k, v in existing.items():
            league_map.setdefault(k, v)

        new_aliases[code] = dict(sorted(league_map.items()))
        status = "OK" if not misses else f"缺 {len(misses)}"
        print(f"[{code}] API season={season} 球队 {len(api_names)}，"
              f"映射 {len(league_map)}，CSV 池 {len(csv_names)} → {status}")
        if misses:
            report.append(f"[{code}] 未匹配（API season={season}）：")
            for m in misses:
                report.append(f"    {m!r}: \"???\",")
        if fuzzy_hits:
            report.append(f"[{code}] 模糊匹配（请人工核对）：")
            for a, c in fuzzy_hits:
                report.append(f"    {a!r} -> {c!r}")

    with open(NEW_PATH, "w", encoding="utf-8") as f:
        f.write("# -*- coding: utf-8 -*-\n")
        f.write('"""team_aliases.py — API-Football 官方队名 → football-data.co.uk 短名。\n')
        f.write("由 rebuild_aliases.py 基于 Pro /teams 当前赛季名单重建；未映射球队整场跳过。\n")
        f.write('"""\n\n')
        f.write("TEAM_ALIASES = {\n")
        for code in us.LEAGUES_A + us.LEAGUES_B:
            f.write(f"    # {code}\n")
            f.write(f"    {code!r}: {{\n")
            for k, v in new_aliases[code].items():
                f.write(f"        {k!r}: {v!r},\n")
            f.write("    },\n")
        f.write("}\n")

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")

    print(f"\n总覆盖：{covered}/{total}（{covered * 100 // max(total, 1)}%）")
    print(f"新表: {NEW_PATH}")
    print(f"报告: {REPORT_PATH}")


if __name__ == "__main__":
    main()
