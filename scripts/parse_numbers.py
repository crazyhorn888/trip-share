#!/usr/bin/env python3
"""
trip-share/scripts/parse_numbers.py
掃描 旅行/ 資料夾內所有 .numbers 檔，解析並 POST 到 N8N Webhook。
trip_id 優先讀「設定」工作表；找不到才從檔名 slug。
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
import warnings
warnings.filterwarnings("ignore")

try:
    import numbers_parser
except ImportError:
    print("ERROR: numbers-parser 未安裝，請執行: pip3 install numbers-parser")
    sys.exit(1)

# ── 設定 ─────────────────────────────────────────────────────────────
WATCH_FOLDER = Path.home() / "Library/Mobile Documents/com~apple~Numbers/Documents/旅行"
N8N_WEBHOOK  = "http://localhost:5678/webhook/trip-sync"
LOCK_FILE    = Path("/tmp/trip-sync.lock")
DEBOUNCE_SEC = 60

# 過渡期保險：設定工作表尚未填寫時的 fallback 對照
TRIP_ID_MAP = {
    "2026.08 紐西蘭": "nz-2026",
}

# ── 工具函式 ──────────────────────────────────────────────────────────
def safe_val(cell):
    v = cell.value
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y/%m/%d %H:%M") if (v.hour or v.minute) else v.strftime("%Y/%m/%d")
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return int(v) if v == int(v) else round(v, 2)
    return str(v).strip()

def parse_sheet_as_dicts(doc, sheet_name: str) -> list:
    for sheet in doc.sheets:
        if sheet.name == sheet_name:
            table = list(sheet.tables)[0]
            rows  = list(table.rows())
            if len(rows) < 2:
                return []
            headers = [safe_val(c) for c in rows[0]]
            result  = []
            for row in rows[1:]:
                vals = [safe_val(c) for c in row]
                if any(v != "" and v is not False for v in vals):
                    result.append(dict(zip(headers, vals)))
            return result
    return []

def parse_settings_sheet(doc) -> dict:
    """讀取「設定」工作表第一列資料（欄位名稱→值）"""
    for sheet in doc.sheets:
        if sheet.name == "設定":
            table = list(sheet.tables)[0]
            rows  = list(table.rows())
            if len(rows) < 2:
                return {}
            headers = [safe_val(c) for c in rows[0]]
            vals    = [safe_val(c) for c in rows[1]]
            return dict(zip(headers, vals))
    return {}

def get_trip_id(settings: dict, filename: str) -> str:
    # 1. 設定工作表
    trip_id = str(settings.get("trip_id", "")).strip()
    if trip_id:
        return trip_id
    # 2. TRIP_ID_MAP（移除 iCloud 版本後綴 -1、-2 後查找）
    stem = Path(filename).stem
    clean_stem = re.sub(r'-\d+$', '', stem).strip()
    if clean_stem in TRIP_ID_MAP:
        return TRIP_ID_MAP[clean_stem]
    # 3. 自動 slug
    return clean_stem.lower().replace(" ", "-").replace(".", "-")

# ── 行程解析 ──────────────────────────────────────────────────────────
def parse_itinerary(rows: list) -> list:
    groups  = []
    current = None
    for row in rows:
        date = str(row.get("日期", "")).strip()
        loc  = str(row.get("地點", "")).strip()
        act  = str(row.get("活動", "")).strip()
        note = str(row.get("附註", "")).strip()
        if date or loc:
            if current:
                groups.append(current)
            current = {"date": date, "location": loc, "activity": act, "note": note,
                       "sub_activities": [], "is_main": True}
        elif act and current:
            current["sub_activities"].append({"activity": act, "note": note})
    if current:
        groups.append(current)
    return groups

def parse_bookings(rows: list) -> list:
    SKIP_FIRST_COL = {"", "total"}
    HIDE_COLS      = {"彰", "君"}
    result = []
    for row in rows:
        name  = str(row.get("", "")).strip()
        total = row.get("總費用", "")
        if name.lower() in SKIP_FIRST_COL and total == "":
            continue
        if name.lower() == "total":
            continue
        clean = {"name": name}
        for k, v in row.items():
            if k not in HIDE_COLS and k != "":
                clean[k] = v
        result.append(clean)
    return result

def parse_cost_split(rows: list) -> list:
    def to_num(v):
        try:
            return float(str(v).replace(",", "")) if v not in ("", None) else 0.0
        except Exception:
            return 0.0
    result = []
    for row in rows:
        item = str(row.get("項目", "")).strip()
        if not item:
            continue
        result.append({
            "item":          item,
            "zeyu_owed":     to_num(row.get("哲宇應付", 0)),
            "zeyu_paid":     to_num(row.get("哲宇已付", 0)),
            "shengmin_owed": to_num(row.get("聖閔應付", 0)),
            "shengmin_paid": to_num(row.get("聖閔已付", 0)),
            "note":          str(row.get("備註", ""))
        })
    return result

def extract_locations(itinerary: list) -> list:
    SKIP = {"台北", "桃園", "待定", "彈性", ""}
    seen = set()
    locs = []
    for item in itinerary:
        raw   = item.get("location", "")
        parts = [p.strip() for p in raw.replace("→", "-").split("-")]
        for p in parts:
            if p and p not in SKIP and not p[0].isdigit() and p not in seen:
                seen.add(p)
                locs.append(p)
    return locs

# ── Debounce ──────────────────────────────────────────────────────────
def check_debounce() -> bool:
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < DEBOUNCE_SEC:
            print(f"[debounce] 距上次觸發 {age:.0f}s，跳過本次")
            return False
    LOCK_FILE.touch()
    return True

def find_all_numbers(folder: Path) -> list:
    """回傳所有 .numbers 檔，依修改時間新→舊排序"""
    return sorted(folder.glob("*.numbers"), key=lambda f: f.stat().st_mtime, reverse=True)

def post_to_n8n(payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req  = urllib.request.Request(
        N8N_WEBHOOK, data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read()
        if raw:
            try:
                result = json.loads(raw)
                print(f"[sync] ✅ N8N 回應: {result}")
            except Exception:
                print(f"[sync] ✅ N8N 回應 (HTTP {resp.status}): {raw[:100]}")
        else:
            print(f"[sync] ✅ N8N 已收到請求 (HTTP {resp.status})")

# ── 主程式 ────────────────────────────────────────────────────────────
def main():
    if not check_debounce():
        sys.exit(0)

    if not WATCH_FOLDER.exists():
        print(f"ERROR: 找不到資料夾 {WATCH_FOLDER}")
        sys.exit(1)

    files = find_all_numbers(WATCH_FOLDER)
    if not files:
        print("ERROR: 旅行/ 內沒有 .numbers 檔案")
        sys.exit(1)

    for nf in files:
        print(f"[sync] 解析: {nf.name}")
        try:
            doc = numbers_parser.Document(str(nf))
        except Exception as e:
            print(f"[sync] ⚠️ 無法解析 {nf.name}: {e}")
            continue

        settings    = parse_settings_sheet(doc)
        trip_id     = get_trip_id(settings, nf.name)
        trip_name   = str(settings.get("trip_name", nf.stem)).strip() or nf.stem
        trip_emoji  = str(settings.get("emoji",     "✈️")).strip()
        trip_status = str(settings.get("status",    "規劃中")).strip()

        raw_itinerary  = parse_sheet_as_dicts(doc, "行程")
        raw_bookings   = parse_sheet_as_dicts(doc, "訂位")
        raw_cost_split = parse_sheet_as_dicts(doc, "費用分擔")

        itinerary  = parse_itinerary(raw_itinerary)
        bookings   = parse_bookings(raw_bookings)
        cost_split = parse_cost_split(raw_cost_split)
        locations  = extract_locations(itinerary)

        date_range = ""
        if itinerary:
            d0 = itinerary[0].get("date", "")
            d1 = itinerary[-1].get("date", "")
            date_range = f"{d0} – {d1}" if d0 != d1 else d0

        payload = {
            "trip_id":     trip_id,
            "trip_name":   trip_name,
            "trip_emoji":  trip_emoji,
            "trip_status": trip_status,
            "date_range":  date_range,
            "locations":   locations,
            "itinerary":   itinerary,
            "bookings":    bookings,
            "cost_split":  cost_split,
        }

        try:
            post_to_n8n(payload)
        except urllib.error.URLError as e:
            print(f"[sync] ❌ N8N 連線失敗 ({nf.name}): {e}")

if __name__ == "__main__":
    main()
