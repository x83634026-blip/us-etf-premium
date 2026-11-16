# -*- coding: utf-8 -*-
"""构建微盘股监测数据（腾讯 + 新浪，不依赖东方财富）。

口径：沪深 A 股中剔除北交所、ST/*ST、退市整理股后，总市值最小的 400 只。
- 股票池与总市值：新浪财经 A 股列表
- 历史日线：腾讯行情（前复权）
- 平均总市值 = 400 只总市值均值；指数 = 400 只等权收益链式（基期 1000）
- 历史段用当前 400 只成分回算，属于近似（存在成分变动与幸存者偏差）
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
CN = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"}
SINA = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeData?page=%d&num=100&sort=symbol&asc=1&node=hs_a")
TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,640,qfq"
START = "2018-01-01"


def _get(url, headers, enc="utf-8", tries=3):
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode(enc, "replace")
        except Exception:
            time.sleep(0.4)
    return ""


def sina_page(page):
    txt = _get(SINA % page, dict(UA, Referer="https://finance.sina.com.cn/"))
    try:
        d = json.loads(txt)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def fetch_universe():
    rows = []
    for page in range(1, 90):
        batch = sina_page(page)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        time.sleep(0.05)
    print("universe:", len(rows))
    if len(rows) < 4500:
        raise SystemExit("universe too small: %d" % len(rows))
    return rows


def is_bse(symbol, code):
    return symbol.startswith("bj") or code.startswith(("4", "8", "92"))


def is_excluded_name(name):
    n = unicodedata.normalize("NFKC", name or "").upper()
    return ("ST" in n) or ("退" in n)


def pick(rows, n=400):
    valid = []
    for x in rows:
        symbol, code = x.get("symbol", ""), str(x.get("code", ""))
        name = x.get("name", "")
        if is_bse(symbol, code) or is_excluded_name(name):
            continue
        try:
            cap = float(x.get("mktcap")) * 1e4      # 万元 -> 元
            price = float(x.get("trade"))
        except (TypeError, ValueError):
            continue
        if cap <= 0 or price <= 0:
            continue
        valid.append({"code": code, "symbol": symbol, "name": name,
                      "cap": cap, "price": price, "shares": cap / price})
    valid.sort(key=lambda t: t["cap"])
    print("valid stocks:", len(valid))
    return valid[:n]


def _prev_day(date_str):
    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def kline(symbol):
    bars = {}
    end = ""
    for _ in range(6):
        txt = _get(TENCENT % (symbol, "", end), UA)
        try:
            data = json.loads(txt).get("data") or {}
        except Exception:
            break
        node = data.get(symbol) or {}
        ks = node.get("qfqday") or node.get("day") or []
        if not ks:
            break
        for row in ks:
            try:
                close = float(row[2])
            except (IndexError, ValueError):
                continue
            if close > 0:
                bars[row[0]] = close
        first = ks[0][0]
        if first <= START:
            break
        end = _prev_day(first)
    return symbol, [(d, bars[d]) for d in sorted(bars) if d >= START]


def tencent_caps(codes):
    """用腾讯行情复核这 400 只的总市值（字段 45，单位亿元）。"""
    syms = [("sh" if c.startswith(("6", "9")) else "sz") + c for c in codes]
    caps = {}
    for i in range(0, len(syms), 60):
        url = "https://qt.gtimg.cn/q=" + ",".join(syms[i:i + 60])
        txt = _get(url, UA, "gb18030")
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

    # 双源校验：新浪总市值 vs 腾讯总市值（同一批 400 只）
    sina_avg = sum(x["cap"] for x in items) / len(items)
    tx = tencent_caps([x["code"] for x in items])
    tx_avg = (sum(tx.values()) / len(tx)) if tx else 0.0
    diff = abs(sina_avg - tx_avg) / sina_avg * 100 if tx_avg else 999.0
    print("cross-check: 新浪 %.2f 亿 (%d 只) | 腾讯 %.2f 亿 (%d 只) | 差 %.3f%%"
          % (sina_avg / 1e8, len(items), tx_avg / 1e8, len(tx), diff))
    if len(tx) < len(items) * 0.9:
        raise SystemExit("tencent caps incomplete: %d/%d" % (len(tx), len(items)))
    if diff > 1.5:
        raise SystemExit("cross-check failed: %.2f%%" % diff)

    hist = {}
    pending = [it["symbol"] for it in items]
    for attempt in range(4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            for symbol, ks in pool.map(kline, pending):
                if ks:
                    hist[symbol] = ks
        pending = [it["symbol"] for it in items if it["symbol"] not in hist]
        print("kline attempt %d: ok %d / %d, missing %d" % (attempt + 1, len(hist), len(items), len(pending)))
        if not pending:
            break
        time.sleep(1.5)

    if len(hist) < len(items) * 0.95:
        raise SystemExit("kline incomplete: %d/%d" % (len(hist), len(items)))

    day_ret = {}
    day_cap = {}
    for it in items:
        ks = hist.get(it["symbol"]) or []
        prev = None
        for date, close in ks:
            if prev is not None and prev > 0:
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

    chg = [float(x.get("changepercent")) for x in rows if isinstance(x.get("changepercent"), (int, float))]
    payload = {
        "updated": dates[-1],
        "built_at": datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S"),
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
             "price": x["price"], "chg": x.get("changepercent")}
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
