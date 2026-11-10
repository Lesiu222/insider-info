import json, os, sys, time, datetime as dt
import xml.etree.ElementTree as ET
import requests

UA = {"User-Agent": "test"}  

# Spolki, ktorych insiderow (Form 4) sledzimy
TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "PLTR",
           "AXP", "BAC", "KO", "CVX", "OXY", "JPM", "COIN", "MSTR",
           "DJT", "LMT", "INTC", "LLY", "BRK-B"]

# Fundusze (13F). Nazwa pochodzi z SEC, wiec zle CIK od razu widac na stronie
FUNDS = [1067983, 1336528, 1697748, 921669]  # Berkshire, Pershing Square, ARK, Icahn

# Politycy wyrozniani (po nazwisku)
VIP = ["Pelosi", "Khanna", "Gottheimer", "Tuberville"]

KEEP_DAYS = 365  # jak dlugo trzymamy archiwum


def get(url, **kw):
    for i in range(4):
        time.sleep(0.15)  # limit SEC: 10 zapytan na sekunde
        try:
            r = requests.get(url, headers=UA, timeout=30, **kw)
        except requests.RequestException:
            if i == 3:
                raise
            time.sleep(2 * (i + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504) and i < 3:
            time.sleep(2 * (i + 1))
            continue
        r.raise_for_status()
        return r


def ln(e):
    return e.tag.split("}")[-1]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def merge(old, new, sort_key, date_of, days=KEEP_DAYS, cap=5000):
    d = {r["id"]: r for r in old if "id" in r}
    d.update({r["id"]: r for r in new})
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = [r for r in d.values() if (date_of(r) or "9999") >= cutoff]
    return sorted(rows, key=sort_key, reverse=True)[:cap]


def load_old():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return {}
    owner, name = repo.split("/", 1)
    try:
        r = requests.get(f"https://{owner.lower()}.github.io/{name}/data.json?t={int(time.time())}", timeout=30)
        if r.status_code == 200:
            return r.json()
        print("Poprzednie dane niedostepne, status", r.status_code)
    except Exception as e:
        print("Poprzednie dane niedostepne:", e)
    return {}


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
    for i, x in enumerate(root.findall(".//nonDerivativeTransaction")):
        code = txt(x, "transactionCoding/transactionCode")
        if code not in ("P", "S"):  # tylko zakupy i sprzedaz na rynku
            continue
        sh = num(txt(x, "transactionAmounts/transactionShares/value"))
        px = num(txt(x, "transactionAmounts/transactionPricePerShare/value"))
        out.append({
            "id": f"{link}#{i}", "ticker": ticker, "person": who, "role": role,
            "type": "Purchase" if code == "P" else "Sale",
            "date": txt(x, "transactionDate/value"), "filed": filed,
            "shares": sh, "price": px, "value": round(sh * px), "link": link,
        })
    return out


def insiders(ciks, seen_old, days=45, per=40):
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows, seen, ok = [], set(), 0
    for t in TICKERS:
        cik = ciks.get(t)
        if not cik:
            print("brak CIK:", t)
            continue
        try:
            rec = get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()["filings"]["recent"]
            ok += 1
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
            if n > per:
                break
            if acc in seen_old:  # juz przetworzone w poprzednich przebiegach
                seen.add(acc)
                continue
            base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"
            try:
                rows += parse4(get(base + doc.split("/")[-1]).content, t, date, base + doc)
                seen.add(acc)
            except Exception as e:
                print(t, acc, e)
    return rows, seen, ok


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
    out, errors = [], 0
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
            errors += 1
            print("fundusz", cik, e)
    return out, errors


# ---------- Politycy (FMP) ----------
def fmp(ep, page, limit, key):
    return requests.get(f"https://financialmodelingprep.com/stable/{ep}",
                        params={"page": page, "limit": limit, "apikey": key}, timeout=30)


def politicians(key):
    rows, count = [], {}
    for chamber, ep in (("Senat", "senate-latest"), ("Izba", "house-latest")):
        first, limit = None, None
        for l in (100, 50, 25, 10, 5):  # plan FMP moze dopuszczac tylko male porcje
            r = fmp(ep, 0, l, key)
            print(ep, "limit", l, "status", r.status_code, "" if r.status_code == 200 else r.text[:150])
            if r.status_code == 200:
                first, limit = r, l
                break
        if not first:
            continue
        for page in range(min(max(1, 400 // limit), 12)):
            r = first if page == 0 else fmp(ep, page, limit, key)
            if r.status_code != 200:
                print(ep, "strona", page, "status", r.status_code)
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
                k = "|".join(str(v) for v in row.values())
                n = count.get(k, 0)
                count[k] = n + 1
                row["id"] = f"{k}#{n}"
                row["dup"] = n > 0  # identyczny wpis: moze byc duplikat API
                rows.append(row)
    return rows


def main():
    contact = os.environ.get("SEC_CONTACT", "").strip()
    if not contact:
        sys.exit("Brak sekretu SEC_CONTACT (np. 'Jan twoj@email.pl'). SEC wymaga kontaktu w nagłówku.")
    UA["User-Agent"] = contact
    os.makedirs("site", exist_ok=True)
    old = load_old()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    failed = []
    data = {"updated": now, "watch": TICKERS, "vip": VIP, "fresh": dict(old.get("fresh", {}))}

    # politycy (mozna wylaczyc zmienna POLITICIANS_ENABLED=0)
    key = os.environ.get("FMP_API_KEY")
    if os.environ.get("POLITICIANS_ENABLED", "1") == "0":
        data["politicians"] = []
    elif not key:
        print("Brak FMP_API_KEY")
        data["politicians"] = old.get("politicians", [])
        failed.append("politicians")
    else:
        try:
            new = politicians(key)
        except Exception as e:
            print("politycy:", e)
            new = []
        if new:
            data["fresh"]["politicians"] = now
        else:
            failed.append("politicians")
        data["politicians"] = merge(old.get("politicians", []), new, lambda r: r["disclosed"], lambda r: r["disclosed"])

    # insiderzy
    seen_old = set(old.get("seen", []))
    ciks = {}
    try:
        ciks = {v["ticker"]: int(v["cik_str"]) for v in get("https://www.sec.gov/files/company_tickers.json").json().values()}
    except Exception as e:
        print("lista CIK:", e)
    new, seen, ok = insiders(ciks, seen_old) if ciks else ([], seen_old, 0)
    if ok < len(TICKERS) * 0.8:
        failed.append("insiders")
    else:
        data["fresh"]["insiders"] = now
    data["insiders"] = merge(old.get("insiders", []), new, lambda r: (r["date"], r["filed"]), lambda r: r["date"])
    data["seen"] = sorted(seen if ok else seen_old)

    # fundusze
    new_f, errs = funds()
    if errs:
        failed.append("funds")
    if new_f and not errs:
        data["fresh"]["funds"] = now
    byname = {f["fund"]: f for f in old.get("funds", [])}
    byname.update({f["fund"]: f for f in new_f})
    data["funds"] = list(byname.values())

    with open("site/data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    for name in failed:
        print(f"::warning::Brak swiezych danych: {name}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"failed={','.join(failed)}\n")
    print({k: len(v) for k, v in data.items() if isinstance(v, list)}, "problemy:", failed)


if __name__ == "__main__":
    main()
