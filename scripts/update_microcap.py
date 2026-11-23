# -*- coding: utf-8 -*-
"""每日增量更新微盘股监测数据（低请求量，双源校验）。

流程：
1. 新浪 A 股列表 -> 剔除北交所/ST/退市 -> 取总市值最小 400 只
2. 腾讯批量行情复核这 400 只的总市值与涨跌幅，双源差值 >1.5% 直接失败
3. 读取仓库里已有的 microcap-data.json，只在末尾追加当天数据点（历史不动）
4. 写回 JSON，交给工作流部署

请求量：新浪约 56 次 + 腾讯约 8 次，通常 20~40 秒完成。
"""
import json
import os
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"}
SINA = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeData?page=%d&num=100&sort=symbol&asc=1&node=hs_a")
TENCENT = "https://qt.gtimg.cn/q="
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("MICROCAP_DATA") or os.path.join(HERE, "..", "site", "microcap", "microcap-data.json")
LIVE = os.environ.get("MICROCAP_LIVE") or "https://us-etf-premium.netlify.app/microcap/microcap-data.json"


def _get(url, enc="utf-8", tries=3):
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=dict(UA, Referer="https://finance.sina.com.cn/"))
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode(enc, "replace")
        except Exception:
            time.sleep(0.6)
    return ""


def fetch_sina():
    rows = []
    for page in range(1, 90):
        txt = _get(SINA % page)
        try:
            batch = json.loads(txt)
        except Exception:
            break
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        time.sleep(0.04)
    return rows


def is_bse(symbol, code):
    return symbol.startswith("bj") or code.startswith(("4", "8", "92"))


def bad_name(name):
    n = unicodedata.normalize("NFKC", name or "").upper()
    return ("ST" in n) or ("退" in n)


def pick(rows, n=400):
    valid = []
    for x in rows:
        code, symbol = str(x.get("code", "")), x.get("symbol", "")
        name = x.get("name", "")
        if is_bse(symbol, code) or bad_name(name):
            continue
        try:
            cap = float(x.get("mktcap")) * 1e4
            price = float(x.get("trade"))
        except (TypeError, ValueError):
            continue
        if cap <= 0 or price <= 0:
            continue
        valid.append({"code": code, "symbol": symbol, "name": name,
                      "cap": cap, "price": price,
                      "chg": float(x.get("changepercent") or 0)})
    valid.sort(key=lambda t: t["cap"])
    return valid[:n]


def tencent_quotes(codes):
    out = {}
    syms = [("sh" if c.startswith(("6", "9")) else "sz") + c for c in codes]
    for i in range(0, len(syms), 60):
        txt = _get(TENCENT + ",".join(syms[i:i + 60]), "gb18030")
        for line in txt.split(";"):
            line = line.strip()
            if not line.startswith("v_"):
                continue
            _, _, val = line.partition("=")
            f = val.strip().strip('"').split("~")
            if len(f) < 46:
                continue
            try:
                out[f[2]] = {"cap": float(f[45]) * 1e8, "chg": float(f[32]), "price": float(f[3])}
            except (TypeError, ValueError):
                continue
    return out


def trade_date():
    txt = _get(TENCENT + "sh000001", "gb18030")
    try:
        f = txt.split("=")[1].strip().strip('";').split("~")
        return "%s-%s-%s" % (f[30][0:4], f[30][4:6], f[30][6:8])
    except Exception:
        return datetime.now(CN).strftime("%Y-%m-%d")


def load_base(path):
    """优先用线上最新数据作为基准，避免每次从仓库里的旧副本重算导致丢天。"""
    local = json.load(open(path, encoding="utf-8"))
    best = local
    try:
        txt = _get(LIVE, tries=2)
        remote = json.loads(txt)
        if remote.get("dates") and local.get("dates") and remote["dates"][-1] > local["dates"][-1]:
            print("use live base:", remote["dates"][-1], ">", local["dates"][-1])
            best = remote
    except Exception:
        pass
    return best


def main():
    path = os.path.abspath(DATA)
    base = load_base(path)
    rows = fetch_sina()
    print("sina rows:", len(rows))
    if len(rows) < 4500:
        raise SystemExit("sina list incomplete: %d" % len(rows))

    items = pick(rows)
    print("constituents:", len(items))
    if len(items) < 400:
        raise SystemExit("not enough valid stocks: %d" % len(items))

    tx = tencent_quotes([x["code"] for x in items])
    sina_avg = sum(x["cap"] for x in items) / len(items)
    tx_avg = (sum(v["cap"] for v in tx.values()) / len(tx)) if tx else 0.0
    diff = abs(sina_avg - tx_avg) / sina_avg * 100 if tx_avg else 999.0
    sina_chg = sum(x["chg"] for x in items) / len(items)
    tx_chg = (sum(v["chg"] for v in tx.values()) / len(tx)) if tx else sina_chg
    ew_chg = (sina_chg + tx_chg) / 2
    print("cross-check: 新浪 %.2f 亿 / 腾讯 %.2f 亿 (差 %.3f%%) | 涨跌 新浪 %.2f%% / 腾讯 %.2f%%"
          % (sina_avg / 1e8, tx_avg / 1e8, diff, sina_chg, tx_chg))
    if len(tx) < len(items) * 0.9:
        raise SystemExit("tencent incomplete: %d/%d" % (len(tx), len(items)))
    if diff > 1.5:
        raise SystemExit("cross-check failed: %.2f%%" % diff)

    today = trade_date()
    dates = base["dates"]
    last = dates[-1]
    if today < last:
        print("no new trading day (%s <= %s), skip" % (today, last))
        return
    if today == last:
        print("same day, refresh last point only")
        base["avgcap"][-1] = round(sina_avg / 1e8, 2)
    else:
        prev_index = base["index"][-1]
        base["dates"].append(today)
        base["index"].append(round(prev_index * (1 + ew_chg / 100.0), 2))
        base["avgcap"].append(round(sina_avg / 1e8, 2))
        base["coverage"].append(len(tx))
        print("appended", today, "index ->", base["index"][-1])

    base["updated"] = today
    base["built_at"] = datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S")
    base["count"] = len(items)
    base["avg_cap"] = round(sina_avg / 1e8, 2)
    base["avg_cap_tx"] = round(tx_avg / 1e8, 2) if tx_avg else None
    base["verify_diff_pct"] = round(diff, 3) if tx_avg else None
    base["verify_count"] = len(tx)
    base["verify_ok"] = bool(tx_avg) and diff <= 1.5
    base["median_cap"] = round(sorted(x["cap"] for x in items)[len(items) // 2] / 1e8, 2)
    base["total_cap"] = round(sum(x["cap"] for x in items) / 1e8, 2)
    base["ew_chg"] = round(ew_chg, 2)
    base["constituents"] = [
        {"code": x["code"], "name": x["name"], "cap": round(x["cap"] / 1e8, 2),
         "price": x["price"], "chg": round(x["chg"], 2)}
        for x in items
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(base, fh, ensure_ascii=False, separators=(",", ":"))
    print("saved", path, os.path.getsize(path), "bytes | updated:", today)


if __name__ == "__main__":
    main()
