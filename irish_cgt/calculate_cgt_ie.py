#!/usr/bin/env python3
"""
Unified Capital Gains Tax (CGT) Calculator — Schwab + Revolut

Accepts one or more CSV files from each broker and produces a combined CGT
summary following Irish tax rules.

Usage:
  python calculate_cgt_ie.py -schwab file1.csv [file2.csv ...] -revolut file3.csv [file4.csv ...]

Either flag is optional — you can pass only -schwab or only -revolut files.

INPUT FILES:
  Schwab:  "Gain/Loss Realized — DETAILED" CSV export from the Schwab portal.
           Must be the Detailed variant (not summary); only the Detailed CSV
           contains per-lot data (Opened Date, Closed Date, Proceeds, Cost Basis).
  Revolut: "Trading P&L Statement" CSV export (en-IE locale).
           The script reads the "Income from Sells" section.
           "Other income & fees" rows (dividends etc.) are counted but excluded
           from CGT — they are income and must be reported separately.

Irish CGT rules applied:
  - CGT rate: 33%
  - Annual personal exemption: €1,270
  - Gain = Proceeds (EUR) − Cost Basis (EUR)
  - Tax = max(0, Total Gain − Exemption) × CGT Rate

FX Conversion — USD→EUR via European Central Bank (ECB) daily reference rates:
  FX@Closed  EUR per 1 USD on the sale date  → applied to Proceeds
  FX@Opened  EUR per 1 USD on the acquisition date  → applied to Cost Basis
  ECB publishes rates on business days only; the script falls back to the
  nearest prior business day when a transaction date falls on a weekend or
  public holiday.
"""

import csv
import os
import sys
from datetime import datetime, timedelta
import urllib.request
import json

# ── USER CONFIGURABLE ────────────────────────────────────────────────────────
CGT_RATE = 0.33           # Capital Gains Tax rate (33%)
ANNUAL_EXEMPTION = 1270.0  # Annual personal exemption in EUR (€1,270)
# ─────────────────────────────────────────────────────────────────────────────

TABLE_WIDTH = 158


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    LIGHT_PURPLE = "\033[38;5;183m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    UNDERLINE = "\033[4m"


def color_gain_loss(gain_pct: float) -> str:
    if gain_pct > 20:
        return Colors.LIGHT_PURPLE
    elif gain_pct >= 0:
        return Colors.GREEN
    elif gain_pct >= -10:
        return Colors.YELLOW
    else:
        return Colors.RED


def fmt_money(val: float, symbol: str, width: int) -> str:
    s = f"-{symbol}{abs(val):,.2f}" if val < 0 else f"{symbol}{val:,.2f}"
    return f"{s:>{width}}"


def fmt_pct(val: float, width: int = 8) -> str:
    return f"{val:>{width - 1}.1f}%"


def parse_schwab_currency(value: str) -> float:
    """Parse Schwab currency strings like '$1,279.97' or '-$36.20'."""
    value = value.strip()
    negative = value.startswith("-")
    value = value.replace("-", "").replace("$", "").replace(",", "")
    return -float(value) if negative else float(value)


def parse_percentage(value: str) -> float:
    return float(value.strip().replace("%", ""))


def parse_date(date_str: str) -> datetime:
    """Parse MM/DD/YYYY (Schwab) or YYYY-MM-DD (Revolut) date strings."""
    date_str = date_str.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {date_str!r}")


def get_ecb_fx_rate(date: datetime) -> float:
    """
    Get USD→EUR rate from ECB for a given date.
    ECB publishes EUR-based rates (USD per 1 EUR), so we invert to get EUR per 1 USD.
    Falls back to previous business days for weekends/holidays.
    """
    for offset in range(0, 10):
        target = date - timedelta(days=offset)
        date_str = target.strftime("%Y-%m-%d")
        url = (
            f"https://data-api.ecb.europa.eu/service/data/EXR/"
            f"D.USD.EUR.SP00.A?startPeriod={date_str}&endPeriod={date_str}"
            f"&format=jsondata"
        )
        try:
            req = urllib.request.Request(url)
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode())
                obs = data["dataSets"][0]["series"]["0:0:0:0:0"]["observations"]
                if obs:
                    usd_per_eur = float(list(obs.values())[0][0])
                    return 1.0 / usd_per_eur
        except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError):
            continue
    raise RuntimeError(f"Could not fetch ECB FX rate near {date.strftime('%Y-%m-%d')}")


# ── FILE LOADERS ──────────────────────────────────────────────────────────────

def load_schwab_file(filepath: str) -> list:
    """Load transactions from a Schwab Realized Gain/Loss DETAILED CSV."""
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    except FileNotFoundError:
        print(f"{Colors.RED}Error: File not found: {filepath}{Colors.RESET}")
        sys.exit(1)

    header_row = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == "Symbol":
            header_row = i
            break

    if header_row is None:
        print(f"{Colors.RED}Error: No 'Symbol' header row in {filepath}{Colors.RESET}")
        sys.exit(1)

    transactions = []
    for row in rows[header_row + 1:]:
        if not row or not row[0].strip():
            continue
        try:
            qty_raw = row[4].strip().replace(",", "")
            qty = float(qty_raw)
            gain_pct = parse_percentage(row[10])
            txn = {
                "symbol": row[0].strip(),
                "name": row[1].strip(),
                "closed_date": parse_date(row[2]),
                "opened_date": parse_date(row[3]),
                "quantity": qty,
                "proceeds_usd": parse_schwab_currency(row[7]),
                "cost_basis_usd": parse_schwab_currency(row[8]),
                "gain_loss_usd": parse_schwab_currency(row[9]),
                "gain_loss_pct": gain_pct,
                "source": "Schwab",
            }
            transactions.append(txn)
        except (ValueError, IndexError):
            continue

    return transactions


def load_revolut_file(filepath: str) -> tuple:
    """
    Load sell transactions from a Revolut Trading P&L Statement CSV.
    Returns (sell_transactions, dividend_row_count).
    Only the "Income from Sells" section is used for CGT; "Other income & fees"
    rows are counted and reported but excluded from CGT calculations.
    """
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    except FileNotFoundError:
        print(f"{Colors.RED}Error: File not found: {filepath}{Colors.RESET}")
        sys.exit(1)

    transactions = []
    dividend_count = 0
    in_sells = False
    in_fees = False
    sells_header = None
    fees_header = None

    for row in rows:
        stripped = [cell.strip() for cell in row]
        first = stripped[0] if stripped else ""

        if first == "Income from Sells":
            in_sells, in_fees, sells_header = True, False, None
            continue
        if first == "Other income & fees":
            in_sells, in_fees, fees_header = False, True, None
            continue

        # Blank row ends the active section
        if not any(stripped):
            in_sells = in_fees = False
            continue

        if in_sells:
            if sells_header is None:
                sells_header = stripped
                continue
            row_dict = {sells_header[i]: stripped[i] if i < len(stripped) else "" for i in range(len(sells_header))}
            try:
                cost = float(row_dict["Cost basis"])
                proceeds = float(row_dict["Gross proceeds"])
                pnl = float(row_dict["Gross PnL"])
                qty = float(row_dict["Quantity"])
                gain_pct = (pnl / cost * 100) if cost != 0 else 0.0
                txn = {
                    "symbol": row_dict["Symbol"],
                    "name": row_dict["Security name"],
                    "closed_date": parse_date(row_dict["Date sold"]),
                    "opened_date": parse_date(row_dict["Date acquired"]),
                    "quantity": qty,
                    "proceeds_usd": proceeds,
                    "cost_basis_usd": cost,
                    "gain_loss_usd": pnl,
                    "gain_loss_pct": gain_pct,
                    "source": "Revolut",
                }
                transactions.append(txn)
            except (ValueError, KeyError):
                continue

        elif in_fees:
            if fees_header is None:
                fees_header = stripped
                continue
            if any(stripped):
                dividend_count += 1

    return transactions, dividend_count


# ── DISPLAY HELPERS ───────────────────────────────────────────────────────────

def print_header(schwab_files: list, revolut_files: list):
    print()
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * TABLE_WIDTH}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}  CAPITAL GAINS TAX (CGT) CALCULATOR — Ireland  [Schwab + Revolut]{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * TABLE_WIDTH}{Colors.RESET}")
    for f in schwab_files:
        print(f"{Colors.DIM}  Schwab:     {os.path.basename(f)}{Colors.RESET}")
    for f in revolut_files:
        print(f"{Colors.DIM}  Revolut:    {os.path.basename(f)}{Colors.RESET}")
    print(f"{Colors.DIM}  CGT Rate:   {CGT_RATE*100:.0f}%   |   Annual Exemption: €{ANNUAL_EXEMPTION:,.2f}{Colors.RESET}")
    print(f"{Colors.DIM}  FX Source:  European Central Bank (ECB) — USD→EUR daily reference rate{Colors.RESET}")
    print(f"{Colors.DIM}  FX@Closed   EUR per 1 USD on the Closed/Sold Date  → applied to Proceeds{Colors.RESET}")
    print(f"{Colors.DIM}  FX@Opened   EUR per 1 USD on the Opened/Acquired Date  → applied to Cost Basis{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * TABLE_WIDTH}{Colors.RESET}")
    print()


def print_source_banner(source: str):
    label = f"  {source.upper()} TRANSACTIONS  "
    pad = (TABLE_WIDTH - len(label)) // 2
    remainder = TABLE_WIDTH - pad - len(label)
    print(f"\n{Colors.BOLD}{Colors.WHITE}{'─' * pad}{label}{'─' * remainder}{Colors.RESET}")


def print_section(title: str):
    print(f"\n{Colors.BOLD}{Colors.BLUE}┌{'─' * (TABLE_WIDTH - 2)}┐{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}│ {title:<{TABLE_WIDTH - 4}} │{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}└{'─' * (TABLE_WIDTH - 2)}┘{Colors.RESET}")


def print_table_header():
    sep = "  "
    header = (
        f"{'Closed':<10}{sep}{'Opened':<10}{sep}{'Qty':>4}{sep}"
        f"{'Proceeds $':>12}{sep}{'Cost Basis $':>12}{sep}"
        f"{'Gain $':>12}{sep}{'Gain%$':>8}{sep}"
        f"{'FX@Closed':>10}{sep}{'FX@Opened':>10}{sep}"
        f"{'Proceeds €':>12}{sep}{'Cost Basis €':>12}{sep}"
        f"{'Gain €*':>12}{sep}{'Gain%€*':>8}"
    )
    print(f"\n  {Colors.BOLD}{Colors.UNDERLINE}{header}{Colors.RESET}")
    print(f"  {Colors.DIM}* EUR Gain is FX-adjusted (Proceeds at Closed/Sold Date rate, Cost Basis at Opened/Acquired Date rate){Colors.RESET}")


# ── CORE PROCESSING ───────────────────────────────────────────────────────────

def process_symbol_group(symbol: str, txns: list, fx_cache: dict) -> tuple:
    """
    Print all transactions for a symbol and return
    (proceeds_eur, cost_eur, gain_eur, proceeds_usd, cost_usd, gain_usd).
    """
    print_section(f"Symbol: {symbol} — {txns[0]['name']}  [{txns[0]['source']}]")
    print_table_header()

    sym_proceeds_eur = sym_cost_eur = sym_gain_eur = 0.0
    sym_proceeds_usd = sym_cost_usd = sym_gain_usd = 0.0

    for txn in txns:
        fx_closed = fx_cache[txn["closed_date"].strftime("%Y-%m-%d")]
        fx_opened = fx_cache[txn["opened_date"].strftime("%Y-%m-%d")]

        proceeds_eur = txn["proceeds_usd"] * fx_closed
        cost_eur = txn["cost_basis_usd"] * fx_opened
        gain_eur = proceeds_eur - cost_eur
        gain_pct_eur = (gain_eur / cost_eur * 100) if cost_eur != 0 else 0.0
        gain_pct_usd = txn["gain_loss_pct"]

        sym_proceeds_eur += proceeds_eur
        sym_cost_eur += cost_eur
        sym_gain_eur += gain_eur
        sym_proceeds_usd += txn["proceeds_usd"]
        sym_cost_usd += txn["cost_basis_usd"]
        sym_gain_usd += txn["gain_loss_usd"]

        color_eur = color_gain_loss(gain_pct_eur)
        color_usd = color_gain_loss(gain_pct_usd)
        sep = "  "

        print(
            f"  {txn['closed_date'].strftime('%m/%d/%Y'):<10}{sep}"
            f"{txn['opened_date'].strftime('%m/%d/%Y'):<10}{sep}"
            f"{txn['quantity']:>4.0f}{sep}"
            f"{fmt_money(txn['proceeds_usd'], '$', 12)}{sep}"
            f"{fmt_money(txn['cost_basis_usd'], '$', 12)}{sep}"
            f"{color_usd}{fmt_money(txn['gain_loss_usd'], '$', 12)}{Colors.RESET}{sep}"
            f"{color_usd}{fmt_pct(gain_pct_usd)}{Colors.RESET}{sep}"
            f"{fx_closed:>10.6f}{sep}"
            f"{fx_opened:>10.6f}{sep}"
            f"{fmt_money(proceeds_eur, '€', 12)}{sep}"
            f"{fmt_money(cost_eur, '€', 12)}{sep}"
            f"{color_eur}{fmt_money(gain_eur, '€', 12)}{Colors.RESET}{sep}"
            f"{color_eur}{fmt_pct(gain_pct_eur)}{Colors.RESET}"
        )

    sym_gain_pct_eur = (sym_gain_eur / sym_cost_eur * 100) if sym_cost_eur != 0 else 0.0
    sym_gain_pct_usd = (sym_gain_usd / sym_cost_usd * 100) if sym_cost_usd != 0 else 0.0
    color_eur = color_gain_loss(sym_gain_pct_eur)
    color_usd = color_gain_loss(sym_gain_pct_usd)
    sep = "  "

    print(f"  {Colors.DIM}{'─' * (TABLE_WIDTH - 2)}{Colors.RESET}")
    print(
        f"  {Colors.BOLD}{'SUBTOTAL':<10}{Colors.RESET}{sep}"
        f"{'':>10}{sep}{'':>4}{sep}"
        f"{fmt_money(sym_proceeds_usd, '$', 12)}{sep}"
        f"{fmt_money(sym_cost_usd, '$', 12)}{sep}"
        f"{color_usd}{fmt_money(sym_gain_usd, '$', 12)}{Colors.RESET}{sep}"
        f"{color_usd}{fmt_pct(sym_gain_pct_usd)}{Colors.RESET}{sep}"
        f"{'':>10}{sep}{'':>10}{sep}"
        f"{fmt_money(sym_proceeds_eur, '€', 12)}{sep}"
        f"{fmt_money(sym_cost_eur, '€', 12)}{sep}"
        f"{color_eur}{fmt_money(sym_gain_eur, '€', 12)}{Colors.RESET}{sep}"
        f"{color_eur}{fmt_pct(sym_gain_pct_eur)}{Colors.RESET}"
    )

    return sym_proceeds_eur, sym_cost_eur, sym_gain_eur, sym_proceeds_usd, sym_cost_usd, sym_gain_usd


def print_summary(
    schwab_eur: tuple,
    revolut_eur: tuple,
    total_eur: tuple,
    dividend_count: int,
):
    lw, vw = 40, 15

    print(f"\n\n{Colors.BOLD}{Colors.CYAN}{'═' * TABLE_WIDTH}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}  CGT SUMMARY{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * TABLE_WIDTH}{Colors.RESET}\n")

    # Per-source breakdown (only shown when both sources have data)
    sources_with_data = []
    if schwab_eur[0] != 0 or schwab_eur[2] != 0:
        sources_with_data.append(("Schwab", schwab_eur))
    if revolut_eur[0] != 0 or revolut_eur[2] != 0:
        sources_with_data.append(("Revolut", revolut_eur))

    if len(sources_with_data) > 1:
        print(f"  {Colors.BOLD}Source Breakdown:{Colors.RESET}")
        for name, (proc, cost, gain) in sources_with_data:
            g_color = Colors.GREEN if gain >= 0 else Colors.RED
            print(
                f"    {Colors.DIM}{name + ':':<{lw - 2}}{Colors.RESET}"
                f"  Proceeds: {Colors.WHITE}€{proc:>{vw - 2},.2f}{Colors.RESET}"
                f"   Gain: {g_color}€{gain:>{vw - 2},.2f}{Colors.RESET}"
            )
        print(f"\n  {Colors.DIM}{'─' * 60}{Colors.RESET}")

    total_proceeds_eur, total_cost_eur, total_gain_eur = total_eur

    print(f"  {Colors.BOLD}{'Total Proceeds (EUR):':<{lw}}{Colors.RESET}"
          f"{Colors.WHITE}€{total_proceeds_eur:>{vw},.2f}{Colors.RESET}")
    print(f"  {Colors.BOLD}{'Total Cost Basis (EUR):':<{lw}}{Colors.RESET}"
          f"{Colors.WHITE}€{total_cost_eur:>{vw},.2f}{Colors.RESET}")

    g_color = Colors.GREEN if total_gain_eur >= 0 else Colors.RED
    print(f"  {Colors.BOLD}{'Total Gain/Loss (EUR):':<{lw}}{Colors.RESET}"
          f"{g_color}€{total_gain_eur:>{vw},.2f}{Colors.RESET}")

    print(f"\n  {Colors.DIM}{'─' * 60}{Colors.RESET}")

    taxable = max(0.0, total_gain_eur - ANNUAL_EXEMPTION) if total_gain_eur > 0 else 0.0
    cgt = taxable * CGT_RATE
    net = total_proceeds_eur - cgt

    print(f"  {Colors.BOLD}{'Annual Exemption:':<{lw}}{Colors.RESET}"
          f"{Colors.WHITE}€{ANNUAL_EXEMPTION:>{vw},.2f}{Colors.RESET}")
    print(f"  {Colors.BOLD}{'Taxable Gain (after exemption):':<{lw}}{Colors.RESET}"
          f"{Colors.WHITE}€{taxable:>{vw},.2f}{Colors.RESET}")

    print(f"\n  {Colors.DIM}{'─' * 60}{Colors.RESET}")

    print(f"  {Colors.BOLD}{Colors.RED}{'CGT Payable (' + f'{CGT_RATE*100:.0f}%' + '):':<{lw}}{Colors.RESET}"
          f"{Colors.RED}€{cgt:>{vw},.2f}{Colors.RESET}")

    if dividend_count > 0:
        print(f"\n  {Colors.DIM}{'─' * 60}{Colors.RESET}")
        print(f"  {Colors.YELLOW}Note: {dividend_count} dividend/fee item(s) found in Revolut file(s) — excluded from CGT.{Colors.RESET}")
        print(f"  {Colors.DIM}  Dividends are income, not capital gains. Report them separately on your tax return.{Colors.RESET}")

    print(f"\n  {Colors.DIM}{'─' * 60}{Colors.RESET}")
    print(f"  {Colors.DIM}Payment & filing deadlines (Irish CGT):{Colors.RESET}")
    print(f"  {Colors.DIM}  • Disposals Jan–Nov  → pay by 15 December of the same year{Colors.RESET}")
    print(f"  {Colors.DIM}  • Disposals in Dec   → pay by 31 January of the following year{Colors.RESET}")
    print(f"  {Colors.DIM}  • Tax return          → file by 31 October of the following year{Colors.RESET}")
    print(f"  {Colors.DIM}  • Must file even if no tax is due{Colors.RESET}")
    print(f"  {Colors.DIM}  • Pay via Revenue Online Service (ROS) or myAccount{Colors.RESET}")
    print(f"  {Colors.DIM}  ↳ Full details: https://www.citizensinformation.ie/en/money-and-tax/tax/capital-taxes/capital-gains-tax/{Colors.RESET}")

    print(f"\n  {Colors.DIM}{'─' * 60}{Colors.RESET}")
    print(f"  {Colors.BOLD}{Colors.GREEN}{'Net Amount After Tax (EUR):':<{lw}}{Colors.RESET}"
          f"{Colors.GREEN}€{net:>{vw},.2f}{Colors.RESET}")

    print(f"\n{Colors.BOLD}{Colors.CYAN}{'═' * TABLE_WIDTH}{Colors.RESET}\n")

    print(f"  {Colors.DIM}Legend:{Colors.RESET}")
    print(f"    {Colors.LIGHT_PURPLE}■{Colors.RESET} Gain > 20%    "
          f"{Colors.GREEN}■{Colors.RESET} Gain 0-20%    "
          f"{Colors.YELLOW}■{Colors.RESET} Loss 0-10%    "
          f"{Colors.RED}■{Colors.RESET} Loss > 10%")
    print()


# ── ARGUMENT PARSING ──────────────────────────────────────────────────────────

def parse_args() -> tuple:
    """
    Parse -schwab file1 file2 ... and -revolut file1 file2 ... arguments.
    Both flags accept single or double dashes (-schwab / --schwab).
    Returns (schwab_files, revolut_files).
    """
    schwab_files = []
    revolut_files = []
    current = None

    for arg in sys.argv[1:]:
        if arg in ("-schwab", "--schwab"):
            current = schwab_files
        elif arg in ("-revolut", "--revolut"):
            current = revolut_files
        elif arg.startswith("-"):
            print(f"{Colors.RED}Error: Unknown option: {arg}{Colors.RESET}")
            _usage()
            sys.exit(1)
        elif current is not None:
            current.append(arg)
        else:
            print(f"{Colors.RED}Error: Unexpected argument before -schwab or -revolut: {arg}{Colors.RESET}")
            _usage()
            sys.exit(1)

    if not schwab_files and not revolut_files:
        print(f"{Colors.RED}Error: No input files provided.{Colors.RESET}")
        _usage()
        sys.exit(1)

    return schwab_files, revolut_files


def _usage():
    print(f"Usage: {os.path.basename(sys.argv[0])} "
          f"-schwab file1.csv [file2.csv ...] "
          f"-revolut file3.csv [file4.csv ...]")
    print("  Either flag is optional; at least one file must be supplied.")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    schwab_files, revolut_files = parse_args()

    all_schwab_txns = []
    for f in schwab_files:
        txns = load_schwab_file(f)
        all_schwab_txns.extend(txns)

    all_revolut_txns = []
    total_dividend_count = 0
    for f in revolut_files:
        txns, div_count = load_revolut_file(f)
        all_revolut_txns.extend(txns)
        total_dividend_count += div_count

    all_txns = all_schwab_txns + all_revolut_txns
    if not all_txns:
        print(f"{Colors.RED}Error: No valid sell transactions found in any input file.{Colors.RESET}")
        sys.exit(1)

    print_header(schwab_files, revolut_files)

    # Collect all unique dates then fetch FX rates in a single pass
    all_dates = set()
    for txn in all_txns:
        all_dates.add(txn["opened_date"].strftime("%Y-%m-%d"))
        all_dates.add(txn["closed_date"].strftime("%Y-%m-%d"))

    print(f"  {Colors.DIM}Fetching FX rates from ECB for {len(all_dates)} unique dates...{Colors.RESET}")

    fx_cache = {}
    for date_str in sorted(all_dates):
        fx_cache[date_str] = get_ecb_fx_rate(datetime.strptime(date_str, "%Y-%m-%d"))

    # Accumulators
    schwab_proceeds_eur = schwab_cost_eur = schwab_gain_eur = 0.0
    revolut_proceeds_eur = revolut_cost_eur = revolut_gain_eur = 0.0

    if all_schwab_txns:
        print_source_banner("Schwab")
        symbols: dict = {}
        for txn in all_schwab_txns:
            symbols.setdefault(txn["symbol"], []).append(txn)
        for symbol, txns in symbols.items():
            proc, cost, gain, _, _, _ = process_symbol_group(symbol, txns, fx_cache)
            schwab_proceeds_eur += proc
            schwab_cost_eur += cost
            schwab_gain_eur += gain

    if all_revolut_txns:
        print_source_banner("Revolut")
        symbols = {}
        for txn in all_revolut_txns:
            symbols.setdefault(txn["symbol"], []).append(txn)
        for symbol, txns in symbols.items():
            proc, cost, gain, _, _, _ = process_symbol_group(symbol, txns, fx_cache)
            revolut_proceeds_eur += proc
            revolut_cost_eur += cost
            revolut_gain_eur += gain

    print_summary(
        (schwab_proceeds_eur, schwab_cost_eur, schwab_gain_eur),
        (revolut_proceeds_eur, revolut_cost_eur, revolut_gain_eur),
        (
            schwab_proceeds_eur + revolut_proceeds_eur,
            schwab_cost_eur + revolut_cost_eur,
            schwab_gain_eur + revolut_gain_eur,
        ),
        total_dividend_count,
    )


if __name__ == "__main__":
    main()
