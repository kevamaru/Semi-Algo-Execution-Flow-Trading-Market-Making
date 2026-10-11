"""
Parse RIT Performance Report PDFs into one session-level dataset.

Usage:  python scripts/parse_reports.py
Input:  reports/*.pdf  (manual_XX.pdf = discretionary phase, semialgo_XX.pdf = with the tool)
Output: data/sessions.csv, data/tenders.csv
"""
import csv
import re
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
DATA = ROOT / "data"
NUM = re.compile(r"-?[\d,]+\.\d+|-?\d+")


def nums(line):
    return [float(x.replace(",", "")) for x in NUM.findall(line)]


def section(text, start, end):
    i = text.find(start)
    j = text.find(end, i + len(start)) if i >= 0 else -1
    return text[i:j] if i >= 0 and j >= 0 else ""


def parse(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    out = {"session": pdf_path.stem, "phase": "semi-algo" if pdf_path.stem.startswith("semialgo") else "manual"}

    pnl_block = section(text, "P&L Summary", "Security P&L Summary")
    out["pnl"] = nums(pnl_block.splitlines()[2])[0]

    sec_block = section(text, "Security P&L Summary", "Security Orders Summary")
    commissions = 0.0
    for line in sec_block.splitlines():
        if line.startswith(("CRZY", "TAME")):
            commissions += nums(line)[2]
    out["commissions"] = commissions

    ord_block = section(text, "Security Orders Summary", "Trading Limit Fines Summary")
    act = pas = 0.0
    for line in ord_block.splitlines():
        if line.startswith(("CRZY", "TAME")):
            n = nums(line)
            # submitted, act_buy_vol, act_buy_vwap, act_sell_vol, act_sell_vwap,
            # pas_buy_vol, pas_buy_vwap, pas_sell_vol, pas_sell_vwap, ratio, trades, avg_value, final_pos
            act += n[1] + n[3]
            pas += n[5] + n[7]
    out["active_volume"] = act
    out["passive_volume"] = pas
    out["passive_share"] = pas / (act + pas) if act + pas else 0.0

    lim_block = section(text, "Trading Limit Fines Summary", "Case Fines Summary")
    lim_total = [l for l in lim_block.splitlines() if l.startswith("TOTAL")]
    out["limit_fines"] = sum(nums(lim_total[0])) if lim_total else 0.0

    case_block = section(text, "Case Fines Summary", "Tender Summary")
    case_total = [l for l in case_block.splitlines() if l.startswith("TOTAL")]
    out["case_fines"] = nums(case_total[0])[0] if case_total else 0.0
    out["total_fines"] = out["limit_fines"] + out["case_fines"]

    tend_block = section(text, "Tender Summary", "Case Info")
    tenders = []
    lines = tend_block.splitlines()
    for k, line in enumerate(lines):
        if " PRIVATE " not in line:
            continue
        parts = line.split()
        status = parts[-1]
        if status not in ("WON", "DECLINED", "EXPIRED", "LOST"):
            # long statuses wrap around the row, e.g. "TRADING_LIM" / row / "IT"
            prev = lines[k - 1].strip() if k else ""
            status = "TRADING_LIMIT" if prev.startswith("TRADING") or status == "IT" else status
        n = nums(" ".join(parts[:3]))
        tenders.append({"session": out["session"], "offer_tick": int(n[1]), "ticker": parts[4],
                        "quantity": float(parts[5].replace(",", "")), "status": status})
    out["tenders_offered"] = len(tenders)
    out["tenders_won"] = sum(t["status"] == "WON" for t in tenders)
    out["tenders_declined"] = sum(t["status"] == "DECLINED" for t in tenders)
    out["tenders_limit_rejected"] = sum(t["status"] == "TRADING_LIMIT" for t in tenders)
    return out, tenders


def main():
    rows, all_tenders = [], []
    files = sorted(REPORTS.glob("manual_*.pdf")) + sorted(REPORTS.glob("semialgo_*.pdf"))
    for i, f in enumerate(files, 1):
        row, tenders = parse(f)
        row["order"] = i
        rows.append(row)
        all_tenders += tenders
    DATA.mkdir(exist_ok=True)
    cols = ["order", "session", "phase", "pnl", "total_fines", "case_fines", "limit_fines", "commissions",
            "passive_volume", "active_volume", "passive_share", "tenders_offered", "tenders_won",
            "tenders_declined", "tenders_limit_rejected"]
    with open(DATA / "sessions.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(r[k], 4) if isinstance(r[k], float) else r[k]) for k in cols})
    with open(DATA / "tenders.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["session", "offer_tick", "ticker", "quantity", "status"])
        w.writeheader()
        w.writerows(all_tenders)
    print(f"{len(rows)} sessions, {len(all_tenders)} tenders -> data/")


if __name__ == "__main__":
    main()
