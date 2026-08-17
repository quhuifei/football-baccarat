#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_worldcup.py — 一次性导入最近 4 届世界杯（2026/2022/2018/2014）全部完场比赛，
输出 data/WC.js（window.LEAGUE_DATA）+ data/WC.json，与 fetch_history.py 的每联赛文件同构，
前端 index.html 像加载普通联赛一样懒加载它；update_scores.py 不碰它，天然在每小时更新后保留。

数据源：API-Football Pro，GET /fixtures?league=1&season={年份}，一届一次调用。
比分惯例与主站一致：AET/PEN 取常规时间（score.fulltime）比分定庄/闲/和，点球只决定晋级。
日期/时间：fixture.date 为 UTC，按各届东道主固定偏移换算成当地日期时间
（不追夏令时，±1 小时误差可接受；2026 北美三国办赛时区混合，取折中 -5）。

2030 年复用：往 EDITION_UTC_OFFSET 加 {2030: +1}（西班牙/葡萄牙/摩洛哥）并把
EDITIONS 里加上 2030 即可，新队名按告警补进 TEAM_CN_WC。

用法: API_FOOTBALL_KEY=xxx python3 import_worldcup.py
依赖: 仅标准库 + 同目录 update_scores.py（复用 safe_write_output）
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta

from update_scores import safe_write_output

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
WC_JSON_PATH = os.path.join(DATA_DIR, "WC.json")
WC_JS_PATH = os.path.join(DATA_DIR, "WC.js")

APIFB_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
APIFB_URL = "https://v3.football.api-sports.io/fixtures?league=1&season={year}"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) import_worldcup/1.0"
TIMEOUT = 40
SLEEP = 1.0

EDITIONS = [2026, 2022, 2018, 2014]
# 各届东道主当地时间相对 UTC 的固定偏移小时（近似，不追夏令时）
EDITION_UTC_OFFSET = {2026: -5, 2022: 3, 2018: 3, 2014: -3}

# API-Football 英文队名 → 中文（覆盖 2014~2026 四届全部参赛队 + 2030 潜在球队常备）
TEAM_CN_WC = {
    "Argentina": "阿根廷", "France": "法国", "Brazil": "巴西", "England": "英格兰",
    "Spain": "西班牙", "Germany": "德国", "Portugal": "葡萄牙", "Netherlands": "荷兰",
    "Belgium": "比利时", "Croatia": "克罗地亚", "Italy": "意大利", "Uruguay": "乌拉圭",
    "Colombia": "哥伦比亚", "Mexico": "墨西哥", "USA": "美国", "Japan": "日本",
    "South Korea": "韩国", "Switzerland": "瑞士", "Morocco": "摩洛哥", "Senegal": "塞内加尔",
    "Denmark": "丹麦", "Poland": "波兰", "Australia": "澳大利亚", "Ecuador": "厄瓜多尔",
    "Qatar": "卡塔尔", "Saudi Arabia": "沙特阿拉伯", "Iran": "伊朗", "Tunisia": "突尼斯",
    "Ghana": "加纳", "Cameroon": "喀麦隆", "Serbia": "塞尔维亚", "Wales": "威尔士",
    "Canada": "加拿大", "Nigeria": "尼日利亚", "Algeria": "阿尔及利亚", "Egypt": "埃及",
    "Chile": "智利", "Peru": "秘鲁", "Sweden": "瑞典", "Iceland": "冰岛",
    "Costa Rica": "哥斯达黎加", "Panama": "巴拿马", "Russia": "俄罗斯", "Greece": "希腊",
    "Honduras": "洪都拉斯", "Ivory Coast": "科特迪瓦", "Bosnia & Herzegovina": "波黑",
    "Paraguay": "巴拉圭", "Austria": "奥地利", "Norway": "挪威", "Scotland": "苏格兰",
    "South Africa": "南非", "New Zealand": "新西兰", "Haiti": "海地", "Jordan": "约旦",
    "Iraq": "伊拉克", "Czechia": "捷克", "Türkiye": "土耳其", "Uzbekistan": "乌兹别克斯坦",
    "Cape Verde Islands": "佛得角", "Curaçao": "库拉索", "Congo DR": "刚果（金）",
    # —— 2030 及以后常备 ——
    "Ukraine": "乌克兰", "China PR": "中国", "North Korea": "朝鲜", "Slovakia": "斯洛伐克",
    "Slovenia": "斯洛文尼亚", "Hungary": "匈牙利", "Romania": "罗马尼亚",
    "Republic of Ireland": "爱尔兰", "Northern Ireland": "北爱尔兰", "Mali": "马里",
    "Burkina Faso": "布基纳法索", "Venezuela": "委内瑞拉", "Bolivia": "玻利维亚",
    "Jamaica": "牙买加", "Bahrain": "巴林", "Oman": "阿曼", "United Arab Emirates": "阿联酋",
    "Kuwait": "科威特", "Syria": "叙利亚", "Lebanon": "黎巴嫩", "Thailand": "泰国",
    "Vietnam": "越南", "Indonesia": "印度尼西亚", "Malaysia": "马来西亚", "India": "印度",
    "Kenya": "肯尼亚", "Zambia": "赞比亚", "Zimbabwe": "津巴布韦", "Ethiopia": "埃塞俄比亚",
    "DR Congo": "刚果（金）", "Congo": "刚果（布）", "Gabon": "加蓬", "Libya": "利比亚",
    "Trinidad and Tobago": "特立尼达和多巴哥", "El Salvador": "萨尔瓦多",
    "Guatemala": "危地马拉", "Cuba": "古巴", "Georgia": "格鲁吉亚", "Albania": "阿尔巴尼亚",
    "North Macedonia": "北马其顿", "Finland": "芬兰", "Belarus": "白俄罗斯",
    "Israel": "以色列", "Kazakhstan": "哈萨克斯坦", "Azerbaijan": "阿塞拜疆",
}

WC_META = {"code": "WC", "name": "FIFA World Cup", "cn": "世界杯",
           "season_type": "calendar"}


def fetch_edition(year):
    req = urllib.request.Request(
        APIFB_URL.format(year=year),
        headers={"x-apisports-key": APIFB_KEY, "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def main():
    if not APIFB_KEY:
        print("错误：未设置 API_FOOTBALL_KEY")
        return 1
    os.makedirs(DATA_DIR, exist_ok=True)

    matches = []
    unmapped = set()
    for year in EDITIONS:
        time.sleep(SLEEP)
        try:
            data = fetch_edition(year)
        except Exception as e:
            print(f"[{year}] 请求失败：{e}，跳过该届")
            continue
        if data.get("errors"):
            print(f"[{year}] API 返回错误 {data['errors']}，跳过该届")
            continue
        off = EDITION_UTC_OFFSET.get(year, 0)
        n = 0
        for f in data.get("response", []):
            status = (f.get("fixture") or {}).get("status", {}).get("short")
            if status not in ("FT", "AET", "PEN"):
                continue
            ft = (f.get("score") or {}).get("fulltime") or {}
            s1, s2 = ft.get("home"), ft.get("away")
            if s1 is None or s2 is None:  # AET/PEN 缺常规时间比分则回退 goals
                goals = f.get("goals") or {}
                s1, s2 = goals.get("home"), goals.get("away")
            if s1 is None or s2 is None:
                continue
            home = (f.get("teams") or {}).get("home", {}).get("name") or ""
            away = (f.get("teams") or {}).get("away", {}).get("name") or ""
            if not home or not away:
                continue
            utc = (f.get("fixture") or {}).get("date") or ""
            if not utc:
                continue
            dt = datetime.fromisoformat(utc.replace("Z", "+00:00")) + timedelta(hours=off)
            ht = (f.get("score") or {}).get("halftime") or {}
            for t in (home, away):
                if t not in TEAM_CN_WC:
                    unmapped.add(t)
            matches.append({
                "date": dt.date().isoformat(),
                "time": dt.strftime("%H:%M"),
                "team1": home, "s1": s1, "s2": s2, "team2": away,
                "result": "B" if s1 > s2 else ("P" if s1 < s2 else "T"),
                "team1_cn": TEAM_CN_WC.get(home, home),
                "team2_cn": TEAM_CN_WC.get(away, away),
                "season": str(year),
                "_league": "WC",
                "ht1": ht.get("home"), "ht2": ht.get("away"),
                "stage": (f.get("league") or {}).get("round") or "",
            })
            n += 1
        print(f"[{year}] 抓到完场 {n} 场（东道主偏移 UTC{off:+d}）")

    if not matches:
        print("错误：一届都没抓到，不写文件")
        return 1
    matches.sort(key=lambda m: (m["date"], m["time"], m["team1"]))

    wc_obj = dict(WC_META)
    wc_obj["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    wc_obj["matches"] = matches
    written = safe_write_output(WC_JSON_PATH, WC_JS_PATH,
                                "LEAGUE_DATA", wc_obj, "matches", "世界杯")

    if unmapped:
        print(f"\n警告：{len(unmapped)} 支球队缺中文名（已兜底用英文名，请补 TEAM_CN_WC）：")
        for t in sorted(unmapped):
            print(f"  - {t}")

    print(f"\n===== 世界杯导入 =====")
    print(f"  合计 {len(matches)} 场（{', '.join(str(y) for y in EDITIONS)} 四届）")
    print(f"  输出文件: {WC_JS_PATH}{'' if written else '（防空写：保留旧文件）'}")
    print(f"  提示: 之后运行 fetch_history.py --rebuild 让 leagues.js 目录带上 WC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
