# -*- coding: utf-8 -*-
"""更新两融余额 / M2 页面数据（两融日度 + M2 月度）。

输出 site/margin/data.json：
{
  "updated": "YYYY-MM-DD",
  "margin_daily": [{"d": "...", "v": 万亿}, ...],
  "m2": [{"m": "YYYY-MM", "v": 万亿}, ...],
  "monthly": [{"m": "...", "margin": 万亿, "m2": 万亿, "ratio": %}, ...]
}
"""
import json
import os
import urllib.request

H = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("MARGIN_OUT") or os.path.join(HERE, "..", "site", "margin", "data.json")


def get(url, tries=3):
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=H)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception:
            continue
    return {}


def fetch_margin():
    rows = []
    for pn in range(1, 8):
        u = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ"
             "&columns=ALL&pageNumber=%d&pageSize=800&sortColumns=DIM_DATE&sortTypes=-1" % pn)
        data = (get(u).get("result") or {}).get("data") or []
        if not data:
            break
        rows.extend(data)
    rows.sort(key=lambda r: r["DIM_DATE"])
    return rows


def fetch_m2():
    u = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_ECONOMY_CURRENCY_SUPPLY"
         "&columns=ALL&pageNumber=1&pageSize=500&sortColumns=REPORT_DATE&sortTypes=-1")
    data = (get(u).get("result") or {}).get("data") or []
    out = [{"m": r["REPORT_DATE"][:7], "v": round(r["BASIC_CURRENCY"] / 1e4, 3)} for r in data]
    out.sort(key=lambda x: x["m"])
    return out


def main():
    rows = fetch_margin()
    m2 = fetch_m2()
    if not rows or not m2:
        raise SystemExit("data empty: margin=%d m2=%d" % (len(rows), len(m2)))

    daily = []
    for r in rows:
        v = r["RZRQYE"] / 1e12
        if v > 0.01:
            daily.append({"d": r["DIM_DATE"][:10], "v": round(v, 4)})

    m2map = {x["m"]: x["v"] for x in m2}
    month_last = {}
    for x in daily:
        month_last[x["d"][:7]] = x["v"]
    monthly = []
    for m in sorted(month_last):
        if m in m2map:
            monthly.append({"m": m, "margin": month_last[m], "m2": m2map[m],
                            "ratio": round(month_last[m] / m2map[m] * 100, 3)})

    out = {"updated": daily[-1]["d"], "margin_daily": daily, "m2": m2, "monthly": monthly}
    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    print("margin daily:", len(daily), "| latest:", daily[-1])
    print("m2:", len(m2), "| latest:", m2[-1])
    print("monthly:", len(monthly), "| latest:", monthly[-1])
    print("saved:", os.path.abspath(OUT), os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
