"""실시간 착순 백업(GitHub Actions용). 맥이 꺼져 live.json 이 10분 넘게 안 바뀌면 대신 착순·확정 배당을 갱신한다.

- 맥(tools/publish_live.py)이 켜져 있으면 아무것도 하지 않는다(당일 갱신 예측은 맥에서만 만든다).
- 오늘 경주가 있는 경마장은 index.html 에 들어 있는 예측 자료(DATA)에서 찾는다. 없으면 API를 부르지 않는다.
- 필요한 것: 저장소 Secrets 의 DATA_GO_KR_SERVICE_KEY. 공개 파일에는 키를 넣지 않는다.
- 착순·배당 계산은 본 저장소 밖 kra_live 모듈(경마 분석 앱)과 같은 규칙을 쓴다.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

KST = timezone(timedelta(hours=9))
SITE = Path(__file__).resolve().parents[2]
LIVE = SITE / "live.json"
BASE = "https://apis.data.go.kr/B551015/"
URL = BASE + "API299/Race_Result_total"
DETAIL = BASE + "API214_1/RaceDetailResult_1"
DIVIDEND = BASE + "API179_1/salesAndDividendRate_1"
POOLS = [("단식", "단승"), ("연식", "연승"), ("복식", "복승"), ("쌍식", "쌍승"), ("복연", "복연승"), ("삼복", "삼복승"), ("삼쌍", "삼쌍승")]
STALE_MIN = 10
WINDOW = ("09:30", "21:00")


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _hhmm(v):
    s = str(v or "")
    if "T" in s:
        return s.split("T")[1][:5]
    if s.isdigit() and len(s) in (3, 4):
        s = s.zfill(4)
        return f"{s[:2]}:{s[2:]}"
    return s[:5] or None


def _odds(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f <= 0 or f >= 9999 else f


def _circ(n) -> str:
    n = _int(n)
    return chr(0x2460 + n - 1) if 1 <= n <= 20 else f"({n})"


def _fmt(v) -> str:
    return f"{float(v):g}"


def fetch(meet: str, d: str, key: str, url: str = URL) -> list[dict]:
    j = requests.get(url, params={"serviceKey": key, "_type": "json", "numOfRows": 1000, "pageNo": 1,
                                  "meet": meet, "rc_date": d}, timeout=60).json()
    if "response" not in j:
        raise RuntimeError("API 오류 응답")
    items = (j["response"].get("body") or {}).get("items") or {}
    it = items.get("item", []) if isinstance(items, dict) else []
    return [it] if isinstance(it, dict) else list(it)


def parse(items: list[dict]) -> list[dict]:
    races: dict[int, list[dict]] = {}
    for x in items:
        races.setdefault(int(x["rcNo"]), []).append(x)
    out = []
    for no in sorted(races):
        rows = races[no]
        first = rows[0]
        placed = sorted((r for r in rows if _int(r.get("ord")) > 0), key=lambda r: _int(r.get("ord")))
        final = any(str(r.get("finalBit")) == "1" for r in rows)
        status = "확정" if final else ("착순 발표(미확정)" if len(placed) >= 2 else "경주 전")
        if status == "경주 전":
            placed = []
        out.append({"rc_no": no, "start": _hhmm(first.get("stTime") or first.get("schStTime")),
                    "start_changed": bool(first.get("schStTime")) and _hhmm(first.get("stTime")) not in (None, _hhmm(first.get("schStTime"))),
                    "status": status, "n": len(rows),
                    "order": [{"ord": _int(r.get("ord")), "chul_no": _int(r.get("chulNo")), "hr_name": r.get("hrName"),
                               "jk_name": r.get("jkName"), "rc_time": r.get("rcTime")} for r in placed]})
    return out


def add_odds(races: list[dict], meet: str, d: str, key: str) -> list[dict]:
    if not any(r["status"] != "경주 전" for r in races):
        return races
    try:
        det = fetch(meet, d, key, DETAIL)
    except Exception:
        det = []
    try:
        div = fetch(meet, d, key, DIVIDEND)
    except Exception:
        div = []
    odds = {(_int(x.get("rcNo")), _int(x.get("chulNo"))): (_odds(x.get("winOdds")), _odds(x.get("plcOdds"))) for x in det}
    pools: dict[int, dict] = {}
    for x in div:
        pools.setdefault(_int(x.get("rcNo")), {})[x.get("pool")] = " ".join(str(x.get("odds") or "").split())
    for r in races:
        if r["status"] == "경주 전":
            continue
        for o in r["order"]:
            o["win_odds"], o["plc_odds"] = odds.get((r["rc_no"], o["chul_no"]), (None, None))
        got = pools.get(r["rc_no"], {})
        r["dividends"] = [{"pool": name, "odds": got[k]} for k, name in POOLS if got.get(k)]
        if not r["dividends"] and r["order"]:
            w, part = r["order"][0], []
            if w.get("win_odds"):
                part.append({"pool": "단승", "odds": f"{_circ(w['chul_no'])}-{_fmt(w['win_odds'])}"})
            plc = [o for o in r["order"][:3] if o.get("plc_odds")]
            if plc:
                part.append({"pool": "연승", "odds": " ".join(f"{_circ(o['chul_no'])}-{_fmt(o['plc_odds'])}" for o in plc)})
            if part:
                r["dividends"], r["dividends_partial"] = part, True
    return races


def meets_today(today: str) -> dict[str, str]:
    """index.html 의 DATA 에서 오늘 경주일이 있는 경마장 → 이름."""
    html = (SITE / "index.html").read_text(encoding="utf-8")
    m = re.search(r"var DATA = (\{.*?\});\n", html, re.S)
    if not m:
        return {}
    data = json.loads(m.group(1))
    cal = data.get("race_calendar") or {}
    return {k: v["name"] for k, v in data.get("meets", {}).items()
            if today in cal.get(k, []) or any(d["date"] == today for d in v.get("days", []))}


def _complete(races) -> bool:
    if not races:
        return False
    done = [r for r in races if r.get("status") != "경주 전"]
    return all(r.get("dividends") and not r.get("dividends_partial") for r in done)


def _strip(o):
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in ("updated_at", "error", "source", "pre_odds_seen")}
    if isinstance(o, list):
        return [_strip(x) for x in o]
    return o


def main() -> int:
    now = datetime.now(KST)
    today = now.strftime("%Y%m%d")
    force = "--force" in sys.argv
    if not force and not (WINDOW[0] <= now.strftime("%H:%M") <= WINDOW[1]):
        print("시간대 밖")
        return 0
    old = json.loads(LIVE.read_text(encoding="utf-8")) if LIVE.exists() else {}
    if old.get("updated_at") and old.get("source") != "actions" and not force:  # 마지막 갱신이 맥이면 맥에 맡긴다
        age = (now - datetime.fromisoformat(old["updated_at"])).total_seconds() / 60
        if age < STALE_MIN:
            print(f"맥이 {age:.0f}분 전에 갱신함 → 건너뜀")
            return 0
    names = meets_today(today)
    if not names:
        print("오늘 경주 없음")
        return 0
    prev_today = (old.get("days") or {}).get(today, {}).get("meets", {})
    if not force and prev_today and all(m in prev_today for m in names) and \
            all(not any(r.get("status") == "경주 전" for r in prev_today[m].get("races") or []) and _complete(prev_today[m].get("races"))
                for m in names):
        print("오늘 경주·확정 배당 모두 반영됨")
        return 0
    key = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()
    if not key:
        print("인증키 없음(Secrets DATA_GO_KR_SERVICE_KEY)")
        return 1
    meets = {}
    for m, name in names.items():
        prev = (prev_today.get(m) or {}).get("races") or []
        try:
            races = add_odds(parse(fetch(m, today, key)), m, today, key)
        except Exception as e:
            meets[m] = {"name": name, "races": prev, "error": type(e).__name__}
            continue
        if not races and prev:
            races = prev
        pday = {r["rc_no"]: r.get("day") for r in prev}
        for r in races:  # 맥이 만든 당일 갱신 예측은 그대로 둔다
            if pday.get(r["rc_no"]):
                r["day"] = pday[r["rc_no"]]
        meets[m] = {"name": name, "races": races, "error": None}
    if not any(v["races"] for v in meets.values()):
        print("받은 경주 없음 → 올리지 않음")
        return 1
    days = dict(old.get("days") or {})
    days[today] = {"meets": meets}
    data = {"date": today, "meets": meets, "days": days}
    if _strip({"date": old.get("date"), "meets": old.get("meets"), "days": old.get("days")}) == _strip(data):
        print("변경 없음")
        return 0
    data["updated_at"] = now.isoformat(timespec="seconds")
    data["source"] = "actions"
    LIVE.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("live.json 갱신: " + ", ".join(f"{v['name']} {sum(r['status'] != '경주 전' for r in v['races'])}/{len(v['races'])}" for v in meets.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
