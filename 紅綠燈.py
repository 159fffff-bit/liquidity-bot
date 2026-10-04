#!/usr/bin/env python3
"""
官方金融指標爬蟲（強化版）- SOFR-IORB 改為「較早日期基準」邏輯
每一筆數據都標註日期

啟動時自動透過 Discord Webhook 將報告以 Embed 形式發送到指定頻道

【GitHub Actions 部署】
- Repository Secrets 設定 DISCORD_WEBHOOKS（多個用逗號或換行分隔）
- 本機可 export DISCORD_WEBHOOKS=... 或填下方 FALLBACK 列表
"""

import os
import requests
import csv
import io
from datetime import datetime
from typing import List, Tuple, Dict, Optional
import time

FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FISCAL_API = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OfficialFinancialDataBot/2.1)",
    "Accept": "application/json, text/csv, */*"
}

# ============================================================
# Discord Webhook（優先讀環境變數 DISCORD_WEBHOOKS）
# GitHub Actions → Settings → Secrets → DISCORD_WEBHOOKS
# ============================================================
DISCORD_WEBHOOKS_FALLBACK: List[str] = [
    # 本機測試可暫時取消註解，正式部署請用 Secrets，勿 commit 真實 URL
    # "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN",
]

EMBED_COLOR = 0x1E90FF


def load_webhooks() -> List[str]:
    """從環境變數或 FALLBACK 載入 webhook（支援逗號 / 換行分隔）"""
    env_val = os.environ.get("DISCORD_WEBHOOKS", "").strip()
    if env_val:
        urls = []
        for part in env_val.replace("\r", "\n").split("\n"):
            for u in part.split(","):
                u = u.strip()
                if u and "YOUR_WEBHOOK" not in u and "YOUR_ID" not in u:
                    urls.append(u)
        if urls:
            return urls
    return [u for u in DISCORD_WEBHOOKS_FALLBACK if u and "YOUR_" not in u]


def fetch_fred_csv(series_id: str, retries: int = 3) -> List[Tuple[str, float]]:
    """從 FRED 取得最新序列，回傳 [(date, value), ...] 最新在前"""
    url = f"{FRED_BASE}?id={series_id}"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            reader = csv.reader(io.StringIO(r.text))
            next(reader)  # skip header
            data = []
            for row in reader:
                if len(row) >= 2 and row[1] not in ("", "."):
                    try:
                        data.append((row[0].strip(), float(row[1])))
                    except ValueError:
                        continue
            data.sort(key=lambda x: x[0], reverse=True)
            return data
        except Exception as e:
            if attempt == retries - 1:
                print(f"[錯誤] FRED {series_id} 失敗: {e}")
                return []
            time.sleep(1.5 * (attempt + 1))
    return []


def get_sofr_iorb() -> Dict:
    """
    新邏輯：
    1. 分別取得 SOFR 與 IORB 的最新日期與數值
    2. 取較早日期作為基準日 (base_date)
    3. 讀取兩項在 base_date 的實際數值
    4. 用同日期數據計算利差
    5. 每一筆都標註日期
    """
    sofr_list = fetch_fred_csv("SOFR")
    iorb_list = fetch_fred_csv("IORB")

    if not sofr_list or not iorb_list:
        return {"error": "無法取得 SOFR 或 IORB"}

    # 1. 各自最新
    sofr_latest_date, sofr_latest_val = sofr_list[0]
    iorb_latest_date, iorb_latest_val = iorb_list[0]

    # 2. 較早日期作為基準
    base_date = min(sofr_latest_date, iorb_latest_date)

    # 建立查詢字典
    sofr_dict = {d: v for d, v in sofr_list}
    iorb_dict = {d: v for d, v in iorb_list}

    # 3. 讀取基準日當天的兩項數據
    sofr_on_base = sofr_dict.get(base_date)
    iorb_on_base = iorb_dict.get(base_date)

    spread_bps = None
    if sofr_on_base is not None and iorb_on_base is not None:
        spread_bps = round((sofr_on_base - iorb_on_base) * 100, 2)

    # 連續轉正天數（從最新共同日期往回）
    common_dates = sorted(set(sofr_dict.keys()) & set(iorb_dict.keys()), reverse=True)
    consecutive_positive = 0
    for d in common_dates:
        s = (sofr_dict[d] - iorb_dict[d]) * 100
        if s > 0:
            consecutive_positive += 1
        else:
            break

    return {
        # 各自最新
        "sofr_latest_val": sofr_latest_val,
        "sofr_latest_date": sofr_latest_date,
        "iorb_latest_val": iorb_latest_val,
        "iorb_latest_date": iorb_latest_date,

        # 基準日（較早日期）
        "base_date": base_date,
        "sofr_on_base": sofr_on_base,
        "iorb_on_base": iorb_on_base,
        "spread_bps": spread_bps,

        # 連續轉正
        "consecutive_positive_days": consecutive_positive,
        "is_positive_streak": consecutive_positive > 0
    }


def get_reserves() -> Dict:
    """銀行準備金（最新週平均 + 最新週三水平）"""
    wresbal = fetch_fred_csv("WRESBAL")
    wrbwfrbl = fetch_fred_csv("WRBWFRBL")

    def to_trillion(val: Optional[float]) -> Optional[float]:
        return round(val / 1_000_000, 3) if val is not None else None

    avg_date, avg_val = (wresbal[0] if wresbal else (None, None))
    wed_date, wed_val = (wrbwfrbl[0] if wrbwfrbl else (None, None))

    avg_t = to_trillion(avg_val)
    return {
        "week_avg_trillion": avg_t,
        "week_avg_end_date": avg_date,
        "wednesday_level_trillion": to_trillion(wed_val),
        "wednesday_date": wed_date,
        "below_2_9": (avg_t < 2.9) if avg_t is not None else None
    }


def get_tga() -> Dict:
    """TGA：優先日度 API，失敗則用最新週度 FRED WTREGEN"""
    try:
        params = {
            "sort": "-record_date",
            "page[size]": 30,
            "fields": "record_date,account_type,close_today_bal"
        }
        r = requests.get(FISCAL_API, params=params, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            data = r.json().get("data", [])
            for item in data:
                acct = str(item.get("account_type", "")).lower()
                if "treasury general account" in acct or "tga" in acct:
                    close = item.get("close_today_bal")
                    if close is not None:
                        close_f = float(close)
                        return {
                            "source": "FiscalData Daily (最新已公布)",
                            "close_billion": close_f,
                            "date": item.get("record_date"),
                            "trillion": round(close_f / 1000, 3)
                        }
    except Exception:
        pass

    wtregen = fetch_fred_csv("WTREGEN")
    if wtregen:
        d, v = wtregen[0]
        return {
            "source": "FRED WTREGEN (最新週度)",
            "close_billion": v,
            "date": d,
            "trillion": round(v / 1000, 3)
        }

    return {"source": "無法取得", "close_billion": None, "date": None, "trillion": None}


def generate_report() -> str:
    """產生符合原提示詞格式的最新報告（SOFR-IORB 使用新邏輯）"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    s = get_sofr_iorb()
    reserves = get_reserves()
    tga = get_tga()

    tga_t = tga.get("trillion") or 0
    approaching_1t = tga_t >= 0.85

    report = f"""【官方最新數據報告】抓取時間：{now_str}
（所有數值均為當天可取得的最新已公布數據，每一筆都標註數據日期）

1. SOFR − IORB 利差
   - SOFR 最新數值與日期：{s.get('sofr_latest_val')}% （數據日期 {s.get('sofr_latest_date')}）
   - IORB 最新數值與日期：{s.get('iorb_latest_val')}% （數據日期 {s.get('iorb_latest_date')}）
   - 基準日期（取兩者較早日期）：{s.get('base_date')}
   - 基準日 SOFR：{s.get('sofr_on_base')}% （數據日期 {s.get('base_date')}）
   - 基準日 IORB：{s.get('iorb_on_base')}% （數據日期 {s.get('base_date')}）
   - 當前利差（bps）= 基準日 SOFR − 基準日 IORB：{s.get('spread_bps')} bps
   - 是否已連續轉正（連續多少個交易日 > 0）：{'是，連續 ' + str(s.get('consecutive_positive_days')) + ' 個交易日' if s.get('is_positive_streak') else '否（目前 ≤ 0）'}
   - 更新頻率：SOFR 每個營業日約 8:00 ET（通常落後 1 個營業日）；IORB 每日

2. 銀行準備金
   - 最新週平均數值（兆美元）與結束日期：{reserves.get('week_avg_trillion')} 兆美元（結束 {reserves.get('week_avg_end_date')}）
   - 最新週三水平數值（兆美元）與日期：{reserves.get('wednesday_level_trillion')} 兆美元（{reserves.get('wednesday_date')}）
   - 是否已跌破 2.9 兆美元：{'是' if reserves.get('below_2_9') else '否'}
   - 更新頻率與下次發布時間：每週四（H.4.1），下次約下一個週四

3. TGA（財政部一般帳戶）
   - 最新日度/可用收盤餘額（億或兆美元）與日期：{tga.get('close_billion')} 億美元（約 {tga.get('trillion')} 兆）（數據日期 {tga.get('date')}）【來源：{tga.get('source')}】
   - 最新週度（H.4.1 / FRED）數值與日期：同上（最新可用）
   - 是否正在向 1 兆美元靠攏：{'是' if approaching_1t else '否 / 觀察中'}
   - 更新頻率：日度每日更新；週度每週四

注意事項：
- 若今日有新數據尚未發布，以上為「最新已公布數據」。
- SOFR 通常有 1 個營業日落後，IORB 幾乎即時。
- 利差一律使用「較早日期」當天的兩項實際數據計算，確保同日可比。
- 來源僅限官方（FRED / NY Fed / Fed H.4.1 / Treasury FiscalData）。
"""
    return report


def send_to_discord(report: str) -> None:
    """將報告以 Embed 形式發送到所有設定的 Discord Webhook"""
    webhooks = load_webhooks()
    if not webhooks:
        print("[警告] 尚未設定有效的 Discord Webhook")
        print("  → 本機：export DISCORD_WEBHOOKS=... 或填 DISCORD_WEBHOOKS_FALLBACK")
        print("  → GitHub：Settings → Secrets → DISCORD_WEBHOOKS")
        return

    description = report
    if len(description) > 4090:
        description = description[:4080] + "\n\n...(內容過長已截斷)"

    payload = {
        "username": "交通號誌",
        "embeds": [
            {
                "title": "🚦 紅綠燈",
                "description": f"```\n{description}\n```",
                "color": EMBED_COLOR,
                "footer": {
                    "text": f"自動推送 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                },
                "timestamp": datetime.utcnow().isoformat()
            }
        ]
    }

    for i, webhook_url in enumerate(webhooks, 1):
        try:
            r = requests.post(webhook_url, json=payload, timeout=15)
            if r.status_code in (200, 204):
                print(f"[成功] 已發送到 Webhook #{i}")
            else:
                print(f"[失敗] Webhook #{i} 回傳 {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[錯誤] Webhook #{i} 發送失敗: {e}")


if __name__ == "__main__":
    report = generate_report()
    print(report)
    print("\n" + "=" * 50)
    print("開始透過 Discord Webhook 推送報告...")
    send_to_discord(report)
    print("推送流程結束。")
