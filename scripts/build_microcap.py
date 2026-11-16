# -*- coding: utf-8 -*-
"""构建微盘股监测数据。

口径：沪深 A 股中剔除北交所、ST/*ST、退市整理股后，总市值最小的 400 只。
- 平均总市值 = 400 只总市值的算术平均
- 微盘等权指数 = 400 只等权收益的链式指数（基期 1000）
- 历史段使用当前 400 只成分回算（近似，存在成分变动与幸存者偏差）
"""
import concurrent.futures
import json
import os
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

OUT = os.environ.get("MICROCAP_OUT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "microcap-data.json")
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def get_json(url, tries=3):
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception:
            continue
    return {}


def spot_page(pn):
    q = {"pn": pn, "pz": 100, "po": 0, "np": 1, "fltt": 2, "invt": 2, "fid": "f20",
         "fs": FS, "fields": "f12,f13,f14,f20,f2,f3"}
    return get_json("https://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(q))


def fetch_universe():
    first = spot_page(1)
    total = (first.get("data") or {}).get("total") or 0
    pages = (total + 99) // 100
    rows = []
    for attempt in range(4):
        rows = list((first.get("data") or {}).get("diff") or [])
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            for d in pool.map(spot_page, range(2, pages + 1)):
                rows.extend((d.get("data") or {}).get("diff") or [])
        print("universe attempt %d: %d / %d" % (attempt + 1, len(rows), total))
        if len(rows) >= total * 0.95:
            return rows
        time.sleep(2)
    raise SystemExit("universe incomplete: %d/%d" % (len(rows), total))


def is_bse(code):
    return code.startswith(("4", "8", "92"))


def is_excluded_name(name):
    """ST / *ST / SST / S*ST / 退市整理（xx退、退市xx）等一律剔除。"""
    n = unicodedata.normalize("NFKC", name or "").upper()
    return ("ST" in n) or ("退" in n)


def pick(rows, n=400):
    valid = []
    for x in rows:
        code, name = x.get("f12"), x.get("f14") or ""
        try:
            cap = float(x.get("f20"))
            price = float(x.get("f2"))
        except (TypeError, ValueError):
            continue
        if is_bse(code) or cap <= 0 or price <= 0 or is_excluded_name(name):
            continue
        valid.append({
            "code": code, "name": name, "cap": cap, "price": price,
            "chg": x.get("f3"), "shares": cap / price,
        })
    valid.sort(key=lambda t: t["cap"])
    return valid[:n]


def kline(code):
    market = "1" if code.startswith(("6", "5", "9")) else "0"
    q = {"secid": "%s.%s" % (market, code), "klt": 101, "fqt": 1,
         "beg": "20150101", "end": "20500101",
         "fields1": "f1,f2,f3,f4,f5,f6",
         "fields2": "f51,f52,f53,f54,f55,f56,f57,f58"}
    d = get_json("https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(q))
    ks = (d.get("data") or {}).get("klines") or []
    out = []
    for k in ks:
        p = k.split(",")
        try:
            out.append((p[0], float(p[2])))
        except (IndexError, ValueError):
            continue
    return code, out


def tencent_caps(codes):
    """用腾讯行情复核这 400 只的总市值（字段 45，单位亿元）。"""
    syms = [("sh" if c.startswith(("6", "9")) else "sz") + c for c in codes]
    caps = {}
    for i in range(0, len(syms), 60):
        url = "https://qt.gtimg.cn/q=" + ",".join(syms[i:i + 60])
        txt = ""
        for _ in range(3):
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=20) as r:
                    txt = r.read().decode("gb18030", "replace")
                break
            except Exception:
                time.sleep(0.5)
        for line in txt.split(";"):
            line = line.strip()
            if not line.startswith("v_"):
                continue
            _, _, val = line.partition("=")
            f = val.strip().strip('"').split("~")
            if len(f) < 46:
                continue
            try:
                cap_yi = float(f[45])
            except (TypeError, ValueError):
                continue
            if cap_yi > 0:
                caps[f[2]] = cap_yi * 1e8
    return caps


def main():
    rows = fetch_universe()
    items = pick(rows)
    print("constituents:", len(items))

    # 双源校验：东财总市值 vs 腾讯总市值
    em_avg = sum(x["cap"] for x in items) / len(items)
    tx = tencent_caps([x["code"] for x in items])
    tx_avg = (sum(tx.values()) / len(tx)) if tx else 0.0
    diff = abs(em_avg - tx_avg) / em_avg * 100 if tx_avg else 999.0
    print("cross-check: 东财 %.2f 亿 (%d 只) | 腾讯 %.2f 亿 (%d 只) | 差 %.3f%%"
          % (em_avg / 1e8, len(items), tx_avg / 1e8, len(tx), diff))
    if len(tx) < len(items) * 0.9:
        raise SystemExit("tencent caps incomplete: %d/%d" % (len(tx), len(items)))
    if diff > 1.5:
        raise SystemExit("cross-check failed: %.2f%%" % diff)

    hist = {}
    pending = [it["code"] for it in items]
    for attempt in range(4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            for code, ks in pool.map(kline, pending):
                if ks:
                    hist[code] = ks
        pending = [c for c in [it["code"] for it in items] if c not in hist]
        print("kline attempt %d: ok %d / %d, missing %d" % (attempt + 1, len(hist), len(items), len(pending)))
        if not pending:
            break
        time.sleep(1.5)
    if pending:
        print("still missing:", pending[:10])

    day_ret = {}
    day_cap = {}
    for it in items:
        ks = hist.get(it["code"]) or []
        prev = None
        for date, close in ks:
            if prev is not None and prev > 0 and close > 0:
                r = close / prev - 1.0
                a = day_ret.setdefault(date, [0.0, 0])
                a[0] += r
                a[1] += 1
            b = day_cap.setdefault(date, [0.0, 0])
            b[0] += close * it["shares"]
            b[1] += 1
            prev = close

    dates = sorted(set(day_ret) | set(day_cap))
    level = 1000.0
    index, avgcap, coverage = [], [], []
    for d in dates:
        r = day_ret.get(d)
        if r and r[1] >= 20:
            level *= 1.0 + r[0] / r[1]
        index.append(round(level, 2))
        c = day_cap.get(d)
        avgcap.append(round((c[0] / c[1]) / 1e8, 2) if c and c[1] else None)
        coverage.append(c[1] if c else 0)

    start = 0
    for i, cov in enumerate(coverage):
        if cov >= len(items) * 0.5:
            start = i
            break
    dates, index, avgcap, coverage = dates[start:], index[start:], avgcap[start:], coverage[start:]

    print("latest coverage:", coverage[-1], "/", len(items))
    if coverage[-1] < len(items) * 0.95:
        raise SystemExit("coverage too low: %d/%d, refuse to publish" % (coverage[-1], len(items)))

    chg = [float(x["chg"]) for x in items if isinstance(x["chg"], (int, float))]
    payload = {
        "updated": dates[-1],
        "built_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items),
        "avg_cap": round(sum(x["cap"] for x in items) / len(items) / 1e8, 2),
        "avg_cap_tx": round(tx_avg / 1e8, 2) if tx_avg else None,
        "verify_diff_pct": round(diff, 3) if tx_avg else None,
        "verify_count": len(tx),
        "verify_ok": bool(tx_avg) and diff <= 1.5,
        "median_cap": round(sorted(x["cap"] for x in items)[len(items) // 2] / 1e8, 2),
        "total_cap": round(sum(x["cap"] for x in items) / 1e8, 2),
        "ew_chg": round(sum(chg) / len(chg), 2) if chg else None,
        "index": index,
        "avgcap": avgcap,
        "dates": dates,
        "coverage": coverage,
        "constituents": [
            {"code": x["code"], "name": x["name"], "cap": round(x["cap"] / 1e8, 2),
             "price": x["price"], "chg": x["chg"]}
            for x in items
        ],
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print("dates:", len(dates), dates[0], "->", dates[-1])
    print("avg cap:", payload["avg_cap"], "index:", index[-1], "ew_chg:", payload["ew_chg"])
    print("excluded ST/退 remaining:", sum(1 for c in items if is_excluded_name(c["name"])))
    print("saved:", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
