import csv, io, json, os, re, sys, time, datetime as dt
import xml.etree.ElementTree as ET
import requests

UA = {"User-Agent": "test"}  # w main() ustawiany z sekretu SEC_CONTACT

# Spolki, ktorych insiderow (Form 4) sledzimy
TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "PLTR",
           "AXP", "BAC", "KO", "CVX", "OXY", "JPM", "COIN", "MSTR",
           "DJT", "LMT", "INTC", "LLY", "BRK-B",
           "NFLX", "AVGO", "ORCL", "AMD", "MU", "V", "MA", "GS", "BLK",
           "HOOD", "XOM", "RTX", "NOC", "BA", "UNH", "PFE", "WMT", "COST",
           "MCD", "DIS", "NKE", "SBUX", "CRWV", "CRCL", "KHC", "MCO"]

# Zapytania o polityków per spolka zuzywaja limit FMP (250 dziennie), wiec tylko dla pierwszych 21
POL_TICKERS = TICKERS[:21]

# Fundusze (13F). Nazwa pochodzi z SEC, wiec zle CIK od razu widac na stronie
# Berkshire, Pershing Square, ARK, Icahn, Bridgewater, Soros, Duquesne, Third Point
FUNDS = [1067983, 1336528, 1697748, 921669, 1350694, 1029160, 1536411, 1040273]

# Politycy wyrozniani (po nazwisku)
VIP = ["Pelosi", "Khanna", "Gottheimer", "Tuberville", "Trump"]

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
            if not fl:
                print("fundusz", cik, "brak raportow 13F")
                continue
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
FMP_URL = "https://financialmodelingprep.com/stable/"


def fmp(ep, page, limit, key):
    return requests.get(FMP_URL + ep, params={"page": page, "limit": limit, "apikey": key}, timeout=30)


def norm(chamber, d):
    return {
        "chamber": chamber, "name": f"{d.get('firstName', '')} {d.get('lastName', '')}".strip(),
        "region": d.get("district", ""), "owner": d.get("owner", ""),
        "ticker": d.get("symbol", ""), "asset": d.get("assetDescription", ""),
        "type": d.get("type", ""), "amount": d.get("amount", ""),
        "tx": d.get("transactionDate", ""), "disclosed": d.get("disclosureDate", ""),
        "link": d.get("link", ""),
    }


def dedupe_key(r):
    if "rid" in r:
        return r["rid"]
    return "|".join(str(r[k]) for k in ("chamber", "name", "ticker", "type", "amount", "tx", "disclosed"))


def combine(sources):
    """Laczy listy z roznych zapytan bez podwajania tych samych transakcji."""
    best = {}
    for rows in sources:
        groups = {}
        for r in rows:
            groups.setdefault(dedupe_key(r), []).append(r)
        for k, lst in groups.items():
            if k not in best or len(lst) > len(best[k]):
                best[k] = lst
    out = []
    for k, lst in best.items():
        for n, r in enumerate(lst):
            r = dict(r)
            r["id"] = f"{k}#{n}"
            r["dup"] = n > 0  # identyczny wpis: moze byc duplikat API
            out.append(r)
    return out


def latest(key):
    rows = []
    for chamber, ep in (("Senat", "senate-latest"), ("Izba", "house-latest")):
        first, limit = None, None
        for l in (25, 10, 5):  # plan darmowy: limit maksymalnie 25
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
            rows += [norm(chamber, d) for d in data]
    return rows


def by_symbol(key):
    """Transakcje polityków w spółkach z naszej listy (uzupelnia ostatnie 25 wpisow)."""
    rows = []
    for chamber, ep in (("Senat", "senate-trades"), ("Izba", "house-trades")):
        for t in POL_TICKERS:
            time.sleep(0.2)
            try:
                r = requests.get(FMP_URL + ep, params={"symbol": t, "apikey": key}, timeout=30)
            except requests.RequestException as e:
                print(ep, t, e)
                continue
            if r.status_code != 200:
                print(ep, t, "status", r.status_code, r.text[:100] if t == POL_TICKERS[0] else "")
                if r.status_code in (401, 402, 403):
                    break  # plan tego nie dopuszcza, nie marnujemy zapytan
                continue
            rows += [norm(chamber, d) for d in r.json()]
    return rows


# ---------- Prezydent Trump (OGE 278-T, dane z Open Cabinet) ----------
TRUMP_URL = "https://open-cabinet.vercel.app/data/all-transactions.csv"
TRUMP_MAX = 200  # najnowsze transakcje, zeby nie zdominowaly listy polityków


def filing_date(url, fallback):
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4}|\d{2})(?!\d)", url or "")
    if m:
        mo, d, y = m.groups()
        return f"{y if len(y) == 4 else '20' + y}-{mo}-{d}"
    return fallback


def trump_rows():
    r = requests.get(TRUMP_URL, headers=UA, timeout=120)
    r.raise_for_status()
    text = r.text
    while text.startswith("#"):
        text = text.split("\n", 1)[1]
    out = []
    for d in csv.DictReader(io.StringIO(text)):
        name = d.get("official_name", "").lower()
        if "trump" not in name or "donald" not in name:
            continue
        if d.get("source_kind") != "278-T" or d.get("type") not in ("Purchase", "Sale"):
            continue
        if d.get("instrument_type") not in ("common_stock", "etf") or d.get("verificationState", "").startswith("under"):
            continue
        tk = d.get("resolved_ticker") or d.get("ticker")
        if not tk or not d.get("date"):
            continue
        out.append({
            "chamber": "Prezydent", "name": "Donald J. Trump", "region": "", "owner": "",
            "ticker": tk, "asset": d.get("description", ""), "type": d["type"],
            "amount": d.get("amount_range", ""), "tx": d["date"],
            "disclosed": filing_date(d.get("source_filing_url"), d["date"]),
            "link": d.get("source_filing_url", ""), "rid": d.get("recordId", ""),
        })
    out.sort(key=lambda r: (r["tx"], r["rid"]), reverse=True)
    return out[:TRUMP_MAX]


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

    # politycy: FMP (mozna wylaczyc POLITICIANS_ENABLED=0) i Trump (OGE)
    key = os.environ.get("FMP_API_KEY")
    fmp_on = os.environ.get("POLITICIANS_ENABLED", "1") != "0"
    old_pol = old.get("politicians", [])
    sources = []
    if not fmp_on:
        old_pol = [r for r in old_pol if r.get("chamber") == "Prezydent"]
    elif key:
        try:
            sources += [latest(key), by_symbol(key)]
        except Exception as e:
            print("FMP:", e)
    else:
        print("Brak FMP_API_KEY")
        failed.append("politicians")
    try:
        sources.append(trump_rows())
    except Exception as e:
        print("Trump:", e)
        failed.append("trump")
    new = combine(sources)
    if fmp_on and key and not any(r["chamber"] != "Prezydent" for r in new):
        failed.append("politicians")
    if new:
        data["fresh"]["politicians"] = now
    data["politicians"] = merge(combine([old_pol]), new, lambda r: r["disclosed"], lambda r: r["disclosed"])

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
