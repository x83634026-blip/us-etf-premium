# -*- coding: utf-8 -*-
"""Build microcap monitor data.

- 鍙?A 鑲★紙鍓旈櫎鍖椾氦鎵€銆佸墧闄ら€€甯傛暣鐞嗭級甯傚€兼渶灏忕殑 400 鍙?- 骞冲潎鎬诲競鍊?= 400 鍙€诲競鍊肩畻鏈钩鍧?- 鑷缓銆屽井鐩樿偂绛夋潈鎸囨暟銆嶏細400 鍙瓑鏉冦€佹瘡鏃ュ啀骞宠　鐨勬敹鐩婇摼寮忔寚鏁帮紙鍩烘湡 1000锛?- 闄勶細鐢ㄥ綋鍓?400 鍙垚鍒嗚偂鐨勫巻鍙插墠澶嶆潈鏀剁洏浠峰洖绠楃殑杩戜技搴忓垪锛堝浐瀹氭垚鍒嗭級
"""
import concurrent.futures
import json
import os
import urllib.parse
import urllib.request

OUT = os.environ.get("MICROCAP_OUT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "microcap-data.json")
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
    rows = list((first.get("data") or {}).get("diff") or [])
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for d in pool.map(spot_page, range(2, (total + 99) // 100 + 1)):
            rows.extend((d.get("data") or {}).get("diff") or [])
    return rows


def is_bse(code):
    return code.startswith(("4", "8", "92"))


def pick(rows, n=400):
    valid = []
    for x in rows:
        code, name = x.get("f12"), x.get("f14") or ""
        try:
            cap = float(x.get("f20"))
            price = float(x.get("f2"))
        except (TypeError, ValueError):
            continue
        if is_bse(code) or cap <= 0 or price <= 0 or "閫€" in name or "ST" in name.upper():
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


def main():
    rows = fetch_universe()
    items = pick(rows)
    print("constituents:", len(items))

    hist = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for code, ks in pool.map(kline, [it["code"] for it in items]):
            hist[code] = ks
    got = sum(1 for v in hist.values() if v)
    print("kline ok:", got, "/", len(items))

    # 閫愭棩鑱氬悎
    day_ret = {}   # date -> [sum_ret, n]
    day_cap = {}   # date -> [sum_cap, n]
    for it in items:
        ks = hist.get(it["code"]) or []
        prev = None
        for date, close in ks:
            if prev is not None and prev > 0 and close > 0:
                r = close / prev - 1.0
                a = day_ret.setdefault(date, [0.0, 0])
                a[0] += r
                a[1] += 1
            cap = close * it["shares"]
            b = day_cap.setdefault(date, [0.0, 0])
            b[0] += cap
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

    # 鍙繚鐣欒鐩栧害 >= 50% 鐨勫尯闂?    start = 0
    for i, cov in enumerate(coverage):
        if cov >= len(items) * 0.5:
            start = i
            break
    dates, index, avgcap, coverage = dates[start:], index[start:], avgcap[start:], coverage[start:]

    latest = dates[-1]
    chg = [float(x["chg"]) for x in items if isinstance(x["chg"], (int, float))]
    payload = {
        "updated": latest,
        "count": len(items),
        "avg_cap": round(sum(x["cap"] for x in items) / len(items) / 1e8, 2),
        "median_cap": round(items[len(items) // 2]["cap"] / 1e8, 2),
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
    print("saved:", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()

