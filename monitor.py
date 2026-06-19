from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "monitor.db"
DASHBOARD_PATH = ROOT / "dashboard.html"
INDEX_PATH = ROOT / "index.html"

FLOW_METRICS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss"],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForAdditionsToPropertyPlantAndEquipment",
    ],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "depreciation": [
        "DepreciationDepletionAndAmortization",
        "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
    ],
}

INSTANT_METRICS = {
    "inventory": ["InventoryNet", "InventoryNetOfAllowancesCustomerAdvancesAndProgressBillings"],
    "receivables": [
        "AccountsReceivableNetCurrent",
        "AccountsNotesAndLoansReceivableNetCurrent",
    ],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
}


def load_config() -> dict:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config["sec_user_agent"] = os.environ.get("SEC_USER_AGENT", config["sec_user_agent"])
    return config


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS metrics (
            ticker TEXT NOT NULL,
            metric TEXT NOT NULL,
            period_end TEXT NOT NULL,
            period_start TEXT,
            value REAL NOT NULL,
            filed TEXT,
            accession TEXT,
            source TEXT NOT NULL,
            PRIMARY KEY (ticker, metric, period_end, period_start)
        );
        CREATE TABLE IF NOT EXISTS filings (
            ticker TEXT NOT NULL,
            accession TEXT NOT NULL,
            form TEXT NOT NULL,
            filing_date TEXT NOT NULL,
            report_date TEXT,
            description TEXT,
            url TEXT NOT NULL,
            PRIMARY KEY (ticker, accession)
        );
        CREATE TABLE IF NOT EXISTS page_checks (
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            status INTEGER,
            content_hash TEXT,
            changed INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            PRIMARY KEY (name, checked_at)
        );
        CREATE TABLE IF NOT EXISTS runs (
            started_at TEXT PRIMARY KEY,
            finished_at TEXT,
            companies_ok INTEGER DEFAULT 0,
            companies_failed INTEGER DEFAULT 0,
            error TEXT
        );
        """
    )
    return con


def get_json(url: str, user_agent: str, retries: int = 3) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=35) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("请求失败")


def get_page(url: str, user_agent: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=35) as response:
        return response.status, response.read()


def visible_page_hash(body: bytes) -> str:
    """忽略脚本、样式和排版，只比较网页可见正文。"""
    text = body.decode("utf-8", errors="ignore")
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sec_document_url(cik: str, accession: str, document: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}"


def update_filings(con: sqlite3.Connection, company: dict, submissions: dict) -> None:
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    for i, form in enumerate(forms):
        if form not in {"10-Q", "10-K", "8-K", "20-F", "6-K"}:
            continue
        accession = recent["accessionNumber"][i]
        document = recent["primaryDocument"][i]
        con.execute(
            """
            INSERT OR REPLACE INTO filings
            (ticker, accession, form, filing_date, report_date, description, url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company["ticker"], accession, form, recent["filingDate"][i],
                recent.get("reportDate", [""] * len(forms))[i],
                recent.get("primaryDocDescription", [""] * len(forms))[i],
                sec_document_url(company["cik"], accession, document),
            ),
        )


def choose_fact(facts: dict, names: list[str]) -> tuple[str, dict] | tuple[None, None]:
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    candidates = []
    for name in names:
        if name in us_gaap and "USD" in us_gaap[name].get("units", {}):
            fact = us_gaap[name]
            latest_end = max((row.get("end", "") for row in fact["units"]["USD"]), default="")
            candidates.append((latest_end, name, fact))
    if not candidates:
        return None, None
    _, name, fact = max(candidates, key=lambda item: item[0])
    return name, fact


def unique_facts(rows: list[dict]) -> list[dict]:
    selected: dict[tuple, dict] = {}
    for row in rows:
        if row.get("form") not in {"10-Q", "10-K", "20-F", "6-K"} or not row.get("end"):
            continue
        key = (row.get("start"), row["end"])
        if key not in selected or row.get("filed", "") > selected[key].get("filed", ""):
            selected[key] = row
    return list(selected.values())


def derive_quarters(rows: list[dict]) -> list[dict]:
    """把季度、年初至今和全年数据尽量转换为单季度数据。"""
    rows = unique_facts(rows)
    direct: dict[str, dict] = {}
    cumulative: list[dict] = []
    annual: list[dict] = []
    for row in rows:
        if not row.get("start"):
            continue
        days = (dt.date.fromisoformat(row["end"]) - dt.date.fromisoformat(row["start"])).days
        if 65 <= days <= 115:
            direct[row["end"]] = row.copy()
        elif 140 <= days <= 310:
            cumulative.append(row)
        elif 320 <= days <= 390:
            annual.append(row)

    # 用本财年较短的累计值相减，得到第二、第三季度。
    cumulative.sort(key=lambda r: (r["start"], r["end"]))
    by_start: dict[str, list[dict]] = {}
    for row in cumulative:
        by_start.setdefault(row["start"], []).append(row)
    for start, group in by_start.items():
        first_quarters = [
            row for row in direct.values()
            if row.get("start") == start and row["end"] < group[0]["end"]
        ]
        first_quarter = max(first_quarters, key=lambda row: row["end"], default=None)
        prior_value = first_quarter["val"] if first_quarter else None
        prior_end = first_quarter["end"] if first_quarter else None
        for row in group:
            if prior_value is not None and row["end"] not in direct:
                derived = row.copy()
                derived["val"] = row["val"] - prior_value
                derived["start"] = prior_end
                direct[row["end"]] = derived
            prior_value = row["val"]
            prior_end = row["end"]

    # 用全年减去前三个季度，估算第四季度。
    for row in annual:
        fiscal_start = dt.date.fromisoformat(row["start"])
        parts = [r for r in direct.values() if fiscal_start <= dt.date.fromisoformat(r["end"]) <= dt.date.fromisoformat(row["end"])]
        if len(parts) >= 3 and row["end"] not in direct:
            derived = row.copy()
            derived["val"] = row["val"] - sum(r["val"] for r in sorted(parts, key=lambda x: x["end"])[-3:])
            derived["start"] = sorted(parts, key=lambda x: x["end"])[-1]["end"]
            direct[row["end"]] = derived
    return sorted(direct.values(), key=lambda r: r["end"])


def save_metric(con: sqlite3.Connection, ticker: str, metric: str, row: dict, source: str) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO metrics
        (ticker, metric, period_end, period_start, value, filed, accession, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticker, metric, row["end"], row.get("start"), float(row["val"]), row.get("filed"), row.get("accn"), source),
    )


def update_company(con: sqlite3.Connection, company: dict, user_agent: str) -> None:
    cik = company["cik"]
    submissions = get_json(f"https://data.sec.gov/submissions/CIK{cik}.json", user_agent)
    update_filings(con, company, submissions)
    facts = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", user_agent)
    source = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

    for metric, concepts in FLOW_METRICS.items():
        _, fact = choose_fact(facts, concepts)
        if not fact:
            continue
        for row in derive_quarters(fact["units"]["USD"]):
            save_metric(con, company["ticker"], metric, row, source)

    for metric, concepts in INSTANT_METRICS.items():
        _, fact = choose_fact(facts, concepts)
        if not fact:
            continue
        for row in unique_facts(fact["units"]["USD"]):
            if row.get("start"):
                continue
            save_metric(con, company["ticker"], metric, row, source)


def update_pages(con: sqlite3.Connection, pages: list[dict], user_agent: str) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for page in pages:
        status = None
        digest = None
        error = None
        changed = 0
        try:
            status, body = get_page(page["url"], user_agent)
            digest = visible_page_hash(body)
            prior = con.execute(
                "SELECT content_hash FROM page_checks WHERE name=? AND content_hash IS NOT NULL ORDER BY checked_at DESC LIMIT 1",
                (page["name"],),
            ).fetchone()
            changed = int(bool(prior and prior[0] != digest))
        except Exception as exc:  # 页面监控失败不影响SEC主流程
            error = str(exc)[:300]
        con.execute(
            "INSERT INTO page_checks (name,url,checked_at,status,content_hash,changed,error) VALUES (?,?,?,?,?,?,?)",
            (page["name"], page["url"], now, status, digest, changed, error),
        )


def run_update() -> tuple[int, int]:
    config = load_config()
    con = connect_db()
    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    con.execute("INSERT INTO runs(started_at) VALUES (?)", (started,))
    con.commit()
    ok = failed = 0
    errors = []
    for company in config["companies"]:
        try:
            print(f"采集 {company['ticker']} {company['name']}...")
            update_company(con, company, config["sec_user_agent"])
            con.commit()
            ok += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{company['ticker']}: {exc}")
        time.sleep(0.12)
    update_pages(con, config.get("official_pages", []), config["sec_user_agent"])
    finished = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    con.execute(
        "UPDATE runs SET finished_at=?,companies_ok=?,companies_failed=?,error=? WHERE started_at=?",
        (finished, ok, failed, "\n".join(errors), started),
    )
    con.commit()
    con.close()
    print(f"采集完成：成功 {ok}，失败 {failed}")
    return ok, failed


def metric_series(con: sqlite3.Connection, ticker: str, metric: str, limit: int = 12) -> list[tuple[str, float]]:
    rows = con.execute(
        "SELECT period_end,value FROM metrics WHERE ticker=? AND metric=? ORDER BY period_end DESC LIMIT ?",
        (ticker, metric, limit),
    ).fetchall()
    return list(reversed(rows))


def latest_value(con: sqlite3.Connection, ticker: str, metric: str) -> tuple[str, float] | None:
    row = con.execute(
        "SELECT period_end,value FROM metrics WHERE ticker=? AND metric=? ORDER BY period_end DESC LIMIT 1",
        (ticker, metric),
    ).fetchone()
    return row if row else None


def prior_year_value(con: sqlite3.Connection, ticker: str, metric: str, end: str) -> float | None:
    target = dt.date.fromisoformat(end) - dt.timedelta(days=365)
    rows = con.execute(
        "SELECT period_end,value FROM metrics WHERE ticker=? AND metric=? AND period_end<? ORDER BY period_end DESC LIMIT 8",
        (ticker, metric, end),
    ).fetchall()
    candidates = [(abs((dt.date.fromisoformat(e) - target).days), v) for e, v in rows]
    if not candidates or min(candidates)[0] > 45:
        return None
    return min(candidates)[1]


def yoy(con: sqlite3.Connection, ticker: str, metric: str) -> float | None:
    latest = latest_value(con, ticker, metric)
    if not latest:
        return None
    prior = prior_year_value(con, ticker, metric, latest[0])
    if prior in (None, 0):
        return None
    return latest[1] / prior - 1


def gross_margin(con: sqlite3.Connection, ticker: str) -> tuple[float | None, float | None]:
    revenue = latest_value(con, ticker, "revenue")
    gross = latest_value(con, ticker, "gross_profit")
    if not revenue or not gross or revenue[1] == 0:
        return None, None
    current = gross[1] / revenue[1]
    prior_revenue = prior_year_value(con, ticker, "revenue", revenue[0])
    prior_gross = prior_year_value(con, ticker, "gross_profit", gross[0])
    prior = prior_gross / prior_revenue if prior_revenue and prior_gross is not None else None
    return current, prior


def assess_company(con: sqlite3.Connection, company: dict) -> dict:
    ticker = company["ticker"]
    revenue = latest_value(con, ticker, "revenue")
    revenue_yoy = yoy(con, ticker, "revenue")
    inventory_yoy = yoy(con, ticker, "inventory")
    receivables_yoy = yoy(con, ticker, "receivables")
    capex_yoy = yoy(con, ticker, "capex")
    margin, prior_margin = gross_margin(con, ticker)
    warnings = []
    severe = []

    if revenue_yoy is not None and revenue_yoy < 0:
        severe.append("收入同比下降")
    if inventory_yoy is not None and revenue_yoy is not None and inventory_yoy > revenue_yoy + 0.20:
        warnings.append("库存增速明显快于收入")
    if receivables_yoy is not None and revenue_yoy is not None and receivables_yoy > revenue_yoy + 0.20:
        warnings.append("应收账款增速明显快于收入")
    if margin is not None and prior_margin is not None and margin < prior_margin - 0.02:
        warnings.append("毛利率同比下降超过2个百分点")
    if company["group"] == "云厂商" and capex_yoy is not None and capex_yoy < 0:
        severe.append("资本开支同比下降")

    status = "红" if severe else "黄" if warnings else "绿" if revenue else "未知"
    return {
        **company,
        "period": revenue[0] if revenue else None,
        "revenue": revenue[1] if revenue else None,
        "revenue_yoy": revenue_yoy,
        "inventory_yoy": inventory_yoy,
        "receivables_yoy": receivables_yoy,
        "capex_yoy": capex_yoy,
        "gross_margin": margin,
        "status": status,
        "reason": "；".join(severe + warnings) or "未触发基础财务预警",
    }


def fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1e9:
        return f"{sign}${value / 1e9:.1f}B"
    if value >= 1e6:
        return f"{sign}${value / 1e6:.1f}M"
    return f"{sign}${value:,.0f}"


def fmt_pct(value: float | None) -> str:
    return "—" if value is None or math.isnan(value) else f"{value * 100:.1f}%"


def line_chart(title: str, series: list[tuple[str, list[tuple[str, float]]]], percent: bool = False) -> str:
    all_points = [(date, value) for _, values in series for date, value in values]
    if not all_points:
        return f"<section class='panel'><h2>{html.escape(title)}</h2><p class='muted'>暂无数据</p></section>"
    labels = sorted({date for date, _ in all_points})[-10:]
    values = [value for _, points in series for date, value in points if date in labels]
    low, high = min(values), max(values)
    if high == low:
        high = low + 1
    width, height, left, top, bottom = 850, 300, 70, 25, 45
    plot_w, plot_h = width - left - 25, height - top - bottom
    colors = ["#2563eb", "#dc2626", "#059669", "#9333ea", "#ea580c", "#0891b2"]
    svg = [f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{html.escape(title)}'>"]
    for i in range(5):
        y = top + plot_h * i / 4
        val = high - (high - low) * i / 4
        label = f"{val * 100:.0f}%" if percent else (f"${val / 1e9:.0f}B" if abs(val) >= 1e9 else f"{val / 1e6:.0f}M")
        svg.append(f"<line x1='{left}' y1='{y:.1f}' x2='{left + plot_w}' y2='{y:.1f}' class='grid'/><text x='{left-8}' y='{y+4:.1f}' text-anchor='end'>{label}</text>")
    for idx, label in enumerate(labels):
        x = left + (plot_w * idx / max(1, len(labels) - 1))
        svg.append(f"<text x='{x:.1f}' y='{height-15}' text-anchor='middle'>{html.escape(label[2:7])}</text>")
    legend_x = left
    for sidx, (name, points) in enumerate(series):
        color = colors[sidx % len(colors)]
        mapped = {date: value for date, value in points}
        coords = []
        for idx, label in enumerate(labels):
            if label not in mapped:
                continue
            x = left + (plot_w * idx / max(1, len(labels) - 1))
            y = top + (high - mapped[label]) / (high - low) * plot_h
            coords.append((x, y))
        if coords:
            path = " ".join(("M" if i == 0 else "L") + f" {x:.1f} {y:.1f}" for i, (x, y) in enumerate(coords))
            svg.append(f"<path d='{path}' fill='none' stroke='{color}' stroke-width='3'/>")
            for x, y in coords:
                svg.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='3.5' fill='{color}'/>")
        svg.append(f"<circle cx='{legend_x+5}' cy='12' r='5' fill='{color}'/><text x='{legend_x+15}' y='16'>{html.escape(name)}</text>")
        legend_x += 125
    svg.append("</svg>")
    return f"<section class='panel'><h2>{html.escape(title)}</h2>{''.join(svg)}</section>"


def generate_dashboard() -> None:
    config = load_config()
    con = connect_db()
    assessments = [assess_company(con, c) for c in config["companies"]]
    counts = {s: sum(a["status"] == s for a in assessments) for s in ["绿", "黄", "红", "未知"]}
    last_run = con.execute("SELECT finished_at,companies_ok,companies_failed,error FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    filings = con.execute(
        "SELECT ticker,form,filing_date,description,url FROM filings ORDER BY filing_date DESC LIMIT 30"
    ).fetchall()
    pages = con.execute(
        """
        SELECT p.name,p.url,p.checked_at,p.status,p.changed,p.error
        FROM page_checks p JOIN (SELECT name,MAX(checked_at) latest FROM page_checks GROUP BY name) x
        ON p.name=x.name AND p.checked_at=x.latest ORDER BY p.name
        """
    ).fetchall()

    cloud = [c for c in config["companies"] if c["group"] == "云厂商"]
    capex_series = [(c["ticker"], metric_series(con, c["ticker"], "capex", 10)) for c in cloud]
    optical = [c for c in config["companies"] if c["group"] == "网络光通信"]
    optical_revenue = [(c["ticker"], metric_series(con, c["ticker"], "revenue", 10)) for c in optical]

    rows = []
    status_class = {"绿": "green", "黄": "yellow", "红": "red", "未知": "gray"}
    for a in assessments:
        rows.append(
            "<tr>"
            f"<td><strong>{html.escape(a['ticker'])}</strong><br><span class='muted'>{html.escape(a['name'])}</span></td>"
            f"<td>{html.escape(a['group'])}</td><td>{a['period'] or '—'}</td><td>{fmt_money(a['revenue'])}</td>"
            f"<td>{fmt_pct(a['revenue_yoy'])}</td><td>{fmt_pct(a['capex_yoy'])}</td>"
            f"<td>{fmt_pct(a['inventory_yoy'])}</td><td>{fmt_pct(a['receivables_yoy'])}</td>"
            f"<td>{fmt_pct(a['gross_margin'])}</td>"
            f"<td><span class='badge {status_class[a['status']]}'>{a['status']}</span></td>"
            f"<td>{html.escape(a['reason'])}</td></tr>"
        )

    filing_rows = "".join(
        f"<tr><td>{html.escape(t)}</td><td>{html.escape(form)}</td><td>{date}</td>"
        f"<td><a href='{html.escape(url)}' target='_blank'>{html.escape(desc or '打开官方文件')}</a></td></tr>"
        for t, form, date, desc, url in filings
    ) or "<tr><td colspan='4'>暂无文件</td></tr>"
    page_rows = "".join(
        f"<tr><td><a href='{html.escape(url)}' target='_blank'>{html.escape(name)}</a></td><td>{checked}</td>"
        f"<td>{status or '失败'}</td><td>{'有变化' if changed else '无变化'}</td><td>{html.escape(error or '')}</td></tr>"
        for name, url, checked, status, changed, error in pages
    ) or "<tr><td colspan='5'>暂无检查记录</td></tr>"
    updated = last_run[0] if last_run else "尚未采集"
    run_note = f"成功 {last_run[1]} / 失败 {last_run[2]}" if last_run else ""

    document = f"""<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>AI硬件周期监控</title>
<meta name='theme-color' content='#172033'><meta name='apple-mobile-web-app-capable' content='yes'>
<meta name='apple-mobile-web-app-status-bar-style' content='black-translucent'>
<meta name='apple-mobile-web-app-title' content='AI周期监控'>
<link rel='manifest' href='./manifest.webmanifest'><link rel='icon' href='./icons/icon-192.svg'>
<style>
:root{{--bg:#f4f7fb;--panel:#fff;--text:#172033;--muted:#667085;--line:#e5e7eb}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 Arial,'Microsoft YaHei',sans-serif}}
main{{max-width:1500px;margin:auto;padding:24px}} h1{{margin:0 0 4px;font-size:28px}} h2{{font-size:18px;margin:0 0 14px}}
.header{{display:flex;align-items:center;justify-content:space-between;gap:12px}} .install{{display:none;border:0;border-radius:9px;padding:10px 14px;background:#2563eb;color:white;font-weight:bold}}
.muted{{color:var(--muted);font-size:12px}} .cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}}
.card,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:0 2px 8px #1018280a}}
.card{{min-width:0}} .card strong{{display:block;font-size:30px}} .card .muted{{display:block;white-space:normal;overflow-wrap:anywhere}} .charts{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse}} th,td{{padding:9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}} th{{background:#f8fafc;position:sticky;top:0}}
.table-wrap{{overflow:auto;max-height:650px}} .badge{{display:inline-block;min-width:34px;padding:3px 8px;border-radius:99px;text-align:center;font-weight:bold}}
.green{{background:#d1fae5;color:#047857}} .yellow{{background:#fef3c7;color:#b45309}} .red{{background:#fee2e2;color:#b91c1c}} .gray{{background:#e5e7eb;color:#4b5563}}
svg{{width:100%;height:auto}} svg text{{font-size:11px;fill:#667085}} .grid{{stroke:#e5e7eb;stroke-width:1}} a{{color:#2563eb;text-decoration:none}}
.section{{margin-top:16px}} .note{{background:#eff6ff;border-left:4px solid #2563eb;padding:12px;margin:12px 0}}
.bottom-nav{{display:none}}
@media(max-width:900px){{.cards{{grid-template-columns:1fr 1fr}} .charts{{grid-template-columns:1fr}}}}
@media(max-width:600px){{body{{padding-bottom:68px}} main{{padding:16px 12px}} h1{{font-size:22px}} .header{{align-items:flex-start}} .cards{{gap:8px;margin:14px 0}} .card{{padding:12px}} .card strong{{font-size:25px}} .panel{{padding:12px;border-radius:10px}} .note{{font-size:12px;margin:10px 0}} .table-wrap{{max-height:70vh}} th,td{{padding:8px 7px;font-size:12px}} th:first-child,td:first-child{{position:sticky;left:0;background:white;z-index:2;box-shadow:2px 0 4px #0000000d}} th:first-child{{background:#f8fafc;z-index:3}} svg{{min-width:620px}} .charts .panel{{overflow-x:auto}} .bottom-nav{{display:flex;position:fixed;z-index:20;bottom:0;left:0;right:0;height:58px;background:#172033;box-shadow:0 -4px 18px #0003;justify-content:space-around;align-items:center;padding-bottom:env(safe-area-inset-bottom)}} .bottom-nav a{{color:#e5e7eb;font-size:12px;text-align:center}}}}
</style></head><body><main>
<div class='header'><div><h1>AI硬件周期监控</h1><div class='muted'>最后更新：{html.escape(str(updated))}　{html.escape(run_note)}</div></div><button class='install' id='installApp'>安装到手机</button></div>
<div id='overview'></div>
<div class='note'>颜色是机械预警，不是买卖建议。红色表示基础财务指标触发规则，必须打开原始财报确认原因。</div>
<div class='cards'>
<div class='card'><span>绿色</span><strong>{counts['绿']}</strong><span class='muted'>未触发基础预警</span></div>
<div class='card'><span>黄色</span><strong>{counts['黄']}</strong><span class='muted'>库存、应收或毛利率异常</span></div>
<div class='card'><span>红色</span><strong>{counts['红']}</strong><span class='muted'>收入或云厂商资本开支下降</span></div>
<div class='card'><span>数据不足</span><strong>{counts['未知']}</strong><span class='muted'>SEC尚无可比数据</span></div></div>
<div class='charts'>{line_chart('云厂商季度资本开支', capex_series)}{line_chart('光通信公司季度收入', optical_revenue)}</div>
<section class='panel' id='companies'><h2>公司指标与预警</h2><div class='table-wrap'><table><thead><tr>
<th>公司</th><th>环节</th><th>报告期</th><th>季度收入</th><th>收入同比</th><th>CapEx同比</th><th>库存同比</th><th>应收同比</th><th>毛利率</th><th>状态</th><th>原因</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
<section class='panel section' id='filings'><h2>最新SEC文件</h2><div class='table-wrap'><table><thead><tr><th>公司</th><th>类型</th><th>日期</th><th>官方原文</th></tr></thead><tbody>{filing_rows}</tbody></table></div></section>
<section class='panel section' id='sources'><h2>官方页面变化</h2><div class='table-wrap'><table><thead><tr><th>页面</th><th>检查时间</th><th>状态</th><th>变化</th><th>错误</th></tr></thead><tbody>{page_rows}</tbody></table></div></section>
<section class='panel section'><h2>信号规则</h2><ul><li>红色：收入同比下降；云厂商资本开支同比下降。</li><li>黄色：库存或应收账款增速高于收入20个百分点以上；毛利率同比下降超过2个百分点。</li><li>绿色：仅表示这些基础规则未触发，不代表估值合理或订单一定强劲。</li></ul></section>
</main><nav class='bottom-nav'><a href='#overview'>概览</a><a href='#companies'>公司</a><a href='#filings'>文件</a><a href='#sources'>来源</a></nav>
<script>
let deferredInstall;
const installButton=document.getElementById('installApp');
window.addEventListener('beforeinstallprompt',event=>{{event.preventDefault();deferredInstall=event;installButton.style.display='block';}});
installButton.addEventListener('click',async()=>{{if(!deferredInstall)return;deferredInstall.prompt();await deferredInstall.userChoice;deferredInstall=null;installButton.style.display='none';}});
if('serviceWorker' in navigator && location.protocol.startsWith('http')){{window.addEventListener('load',()=>navigator.serviceWorker.register('./sw.js'));}}
</script></body></html>"""
    DASHBOARD_PATH.write_text(document, encoding="utf-8")
    INDEX_PATH.write_text(document, encoding="utf-8")
    con.close()
    print(f"仪表盘已生成：{DASHBOARD_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI硬件周期监控")
    parser.add_argument("command", choices=["update", "dashboard", "all"], nargs="?", default="all")
    args = parser.parse_args()
    if args.command in {"update", "all"}:
        run_update()
    if args.command in {"dashboard", "all"}:
        generate_dashboard()


if __name__ == "__main__":
    main()
