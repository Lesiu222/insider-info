import json, os, time, datetime as dt
import xml.etree.ElementTree as ET
import requests

UA = {"User-Agent": "Jan lesiu.praca@gmail.com"}  # SEC wymaga imienia i kontaktu

# Spolki, ktorych insiderow (Form 4) sledzimy
TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "PLTR",
           "AXP", "BAC", "KO", "CVX", "OXY", "JPM", "COIN", "MSTR",
           "DJT", "LMT", "INTC", "LLY", "BRK-B"]

# Fundusze (13F). Nazwa pochodzi z SEC, wiec zle CIK od razu widac na stronie
FUNDS = [1067983, 1336528, 1697748, 921669]  # Berkshire, Pershing Square, ARK, Icahn

# Politycy wyrozniani na liscie (po nazwisku)
VIP = ["Pelosi", "Khanna", "Gottheimer", "Tuberville"]


def get(url, **kw):
    time.sleep(0.15)  # limit SEC: 10 zapytan na sekunde
    r = requests.get(url, headers=UA, timeout=30, **kw)
    r.raise_for_status()
    return r


def ln(e):
    return e.tag.split("}")[-1]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---------- Insiderzy (Form 4) ----------
def parse4(xml, ticker, filed, link):
    root = ET.fromstring(xml)
    txt = lambda n, p: (n.findtext(p) or "").strip() if n is not None else ""
    who = txt(root, ".//reportingOwner/reportingOwnerId/rptOwnerName")
    rel = root.find(".//reportingOwner/reportingOwnerRelationship")
    role = txt(rel, "officerTitle")
    if not role and txt(rel, "isDirector") in ("1", "true"):
        role = "Dyrektor"
    if not role and txt(rel, "isTenPercentOwner") in ("1", "true"):
        role = "Udziałowiec 10%+"
    out = []
    for x in root.findall(".//nonDerivativeTransaction"):
        code = txt(x, "transactionCoding/transactionCode")
        if code not in ("P", "S"):  # tylko zakupy i sprzedaz na rynku
            continue
        sh = num(txt(x, "transactionAmounts/transactionShares/value"))
        px = num(txt(x, "transactionAmounts/transactionPricePerShare/value"))
        out.append({
            "ticker": ticker, "person": who, "role": role,
            "type": "Purchase" if code == "P" else "Sale",
            "date": txt(x, "transactionDate/value"), "filed": filed,
            "shares": sh, "price": px, "value": round(sh * px), "link": link,
        })
    return out


def insiders(ciks, days=45):
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = []
    for t in TICKERS:
        cik = ciks.get(t)
        if not cik:
            print("brak CIK:", t)
            continue
        try:
            rec = get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()["filings"]["recent"]
        except Exception as e:
            print(t, e)
            continue
        n = 0
        for form, acc, date, doc in zip(rec["form"], rec["accessionNumber"], rec["filingDate"], rec["primaryDocument"]):
            if date < cutoff:
                break
            if form != "4":
                continue
            n += 1
            if n > 12:
                break
            base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"
            try:
                rows += parse4(get(base + doc.split("/")[-1]).content, t, date, base + doc)
            except Exception as e:
                print(t, acc, e)
    return sorted(rows, key=lambda r: (r["date"], r["filed"]), reverse=True)


# ---------- Fundusze (13F) ----------
def holdings(cik, acc):
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"
    items = get(base + "index.json").json()["directory"]["item"]
    for it in items:
        name = it["name"]
        if not name.lower().endswith(".xml") or "primary_doc" in name.lower():
            continue
        root = ET.fromstring(get(base + name).content)
        h = {}
        for e in root.iter():
            if ln(e) != "infoTable":
                continue
            d = {ln(c): c for c in e}
            sh = next((num(c.text) for c in d["shrsOrPrnAmt"] if ln(c) == "sshPrnamt"), 0)
            key = d["cusip"].text
            old = h.get(key, (d["nameOfIssuer"].text, 0, 0))
            h[key] = (old[0], old[1] + sh, old[2] + num(d["value"].text))
        if h:
            return h
    return {}


def funds():
    out = []
    for cik in FUNDS:
        try:
            rec = get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()
            r = rec["filings"]["recent"]
            fl = [(a, rd, fd) for f, a, rd, fd in zip(r["form"], r["accessionNumber"], r["reportDate"], r["filingDate"]) if f == "13F-HR"]
            cur = holdings(cik, fl[0][0])
            prev = holdings(cik, fl[1][0]) if len(fl) > 1 else {}
            top = []
            for k, (name, sh, val) in sorted(cur.items(), key=lambda kv: -kv[1][2])[:12]:
                p = prev.get(k)
                chg = "nowa" if not p else round((sh - p[1]) / p[1] * 100, 1) if p[1] else None
                top.append({"name": name, "shares": sh, "value": val, "change": chg})
            out.append({"fund": rec["name"], "period": fl[0][1], "filed": fl[0][2],
                        "total": sum(v[2] for v in cur.values()), "top": top})
        except Exception as e:
            print("fundusz", cik, e)
    return out


# ---------- Politycy (FMP) ----------
def politicians(key):
    rows, seen = [], {}
    for chamber, ep in (("Senat", "senate-latest"), ("Izba", "house-latest")):
        for page in range(4):
            r = requests.get(f"https://financialmodelingprep.com/stable/{ep}",
                             params={"page": page, "limit": 100, "apikey": key}, timeout=30)
            if r.status_code != 200:
                print(ep, "status", r.status_code)
                break
            data = r.json()
            if not data:
                break
            for d in data:
                row = {
                    "chamber": chamber, "name": f"{d.get('firstName', '')} {d.get('lastName', '')}".strip(),
                    "region": d.get("district", ""), "owner": d.get("owner", ""),
                    "ticker": d.get("symbol", ""), "asset": d.get("assetDescription", ""),
                    "type": d.get("type", ""), "amount": d.get("amount", ""),
                    "tx": d.get("transactionDate", ""), "disclosed": d.get("disclosureDate", ""),
                    "link": d.get("link", ""),
                }
                k = tuple(row.values())
                seen[k] = seen.get(k, 0) + 1
                row["dup"] = seen[k] > 1  # identyczny wpis: moze byc duplikat API
                rows.append(row)
    return sorted(rows, key=lambda r: r["disclosed"], reverse=True)[:400]


def main():
    os.makedirs("site", exist_ok=True)
    data = {"updated": dt.datetime.utcnow().isoformat() + "Z", "watch": TICKERS, "vip": VIP,
            "politicians": [], "insiders": [], "funds": []}
    key = os.environ.get("FMP_API_KEY")
    if key:
        try:
            data["politicians"] = politicians(key)
        except Exception as e:
            print("politycy:", e)
    else:
        print("Brak FMP_API_KEY, pomijam polityków")
    try:
        ciks = {v["ticker"]: int(v["cik_str"]) for v in get("https://www.sec.gov/files/company_tickers.json").json().values()}
        data["insiders"] = insiders(ciks)
    except Exception as e:
        print("insiderzy:", e)
    data["funds"] = funds()
    with open("site/data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print({k: len(v) for k, v in data.items() if isinstance(v, list)})


if __name__ == "__main__":
    main()
