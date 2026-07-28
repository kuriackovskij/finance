#!/usr/bin/env python3
"""
Capital Gains Tax (CGT) Calculator for Schwab Realized Gain/Loss CSV exports.

INPUT FILE:
  Use the "Gain/Loss Realized — DETAILED" export from the Schwab portal.
  IMPORTANT: Must be the "Detailed" variant (not the summary export) — only the
  Detailed CSV contains per-lot data including Opened Date, Closed Date,
  Proceeds, and Cost Basis per lot, which are all required for this calculation.

Irish CGT rules applied:
- CGT rate: 33% (standard)
- Annual personal exemption: €1,270
- Gain = Proceeds (EUR) - Cost Basis (EUR)
- Tax = (Total Gain - Exemption) * CGT Rate

FX Conversion — USD to EUR via European Central Bank (ECB) daily reference rates:

  FX@Closed  — EUR per 1 USD on the "Closed Date" (the date the shares were sold).
               Applied to the "Proceeds" column to convert the sale amount into EUR.
               Rationale: what you actually received in USD is worth X EUR on the
               day you sold.

  FX@Opened  — EUR per 1 USD on the "Opened Date" (the date the shares were vested
               / acquired, i.e. your cost basis date).
               Applied to the "Cost Basis" column to convert your original cost into EUR.
               Rationale: what you originally paid in USD is expressed in EUR at the
               exchange rate prevailing when you acquired the asset.

  Because USD/EUR fluctuates, the EUR Gain% can differ significantly from the USD
  Gain% — a position that looks profitable in USD may appear smaller (or larger)
  in EUR, and vice versa.

  ECB publishes rates on business days only; if a transaction date falls on a
  weekend or public holiday the script automatically falls back to the nearest
  prior business day rate.
"""

import csv
import sys
import argparse
from datetime import datetime, timedelta
import urllib.request
import json

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                         USER CONFIGURABLE VARIABLES                         ║
# ╠══════════════════════════════════════════════════════════════════════════════╣
# ║ Adjust these values according to your tax situation                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

CGT_RATE = 0.33            # Capital Gains Tax rate (33%)
ANNUAL_EXEMPTION = 1270.0  # Annual personal exemption in EUR (€1,270)

# ══════════════════════════════════════════════════════════════════════════════════


# ANSI color codes
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
    BG_DARK = "\033[48;5;236m"
    UNDERLINE = "\033[4m"


def color_gain_loss(gain_pct: float) -> str:
    """Return color code based on gain/loss percentage."""
    if gain_pct > 20:
        return Colors.LIGHT_PURPLE
    elif gain_pct >= 0:
        return Colors.GREEN
    elif gain_pct >= -10:
        return Colors.YELLOW
    else:
        return Colors.RED


def fmt_money(val: float, symbol: str, width: int) -> str:
    """Format monetary value with currency symbol into a right-aligned fixed-width string."""
    s = f"-{symbol}{abs(val):,.2f}" if val < 0 else f"{symbol}{val:,.2f}"
    return f"{s:>{width}}"


def fmt_pct(val: float, width: int = 8) -> str:
    """Format percentage into a right-aligned fixed-width string (includes % sign)."""
    return f"{val:>{width - 1}.1f}%"


def parse_currency(value: str) -> float:
    """Parse currency string like '$1,279.97' or '-$36.20' to float."""
    value = value.strip()
    negative = value.startswith("-")
    value = value.replace("-", "").replace("$", "").replace(",", "")
    result = float(value)
    return -result if negative else result


def parse_percentage(value: str) -> float:
    """Parse percentage string like '44.14%' to float."""
    value = value.strip().replace("%", "")
    return float(value)


def parse_date(date_str: str) -> datetime:
    """Parse date in MM/DD/YYYY format."""
    return datetime.strptime(date_str.strip(), "%m/%d/%Y")


def get_ecb_fx_rate(date: datetime) -> float:
    """
    Get USD/EUR exchange rate from ECB for a given date.
    ECB publishes EUR-based rates, so we get how many USD per 1 EUR,
    then invert to get EUR per 1 USD.
    Falls back to previous business days if the date is a weekend/holiday.
    """
    # Try the date and up to 5 previous days (for weekends/holidays)
    for offset in range(0, 10):
        target_date = date - timedelta(days=offset)
        date_str = target_date.strftime("%Y-%m-%d")
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
                observations = data["dataSets"][0]["series"]["0:0:0:0:0"]["observations"]
                if observations:
                    # Rate is USD per 1 EUR; we want EUR per 1 USD
                    usd_per_eur = float(list(observations.values())[0][0])
                    return 1.0 / usd_per_eur
        except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError):
            continue

    raise RuntimeError(f"Could not fetch ECB FX rate for date near {date.strftime('%Y-%m-%d')}")


def print_header():
    """Print script header."""
    print()
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * TABLE_WIDTH}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}  CAPITAL GAINS TAX (CGT) CALCULATOR — Ireland{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * TABLE_WIDTH}{Colors.RESET}")
    print(f"{Colors.DIM}  Input:      Schwab \"Gain/Loss Realized — DETAILED\" CSV export{Colors.RESET}")
    print(f"{Colors.DIM}  CGT Rate:   {CGT_RATE*100:.0f}%   |   Annual Exemption: €{ANNUAL_EXEMPTION:,.2f}{Colors.RESET}")
    print(f"{Colors.DIM}  FX Source:  European Central Bank (ECB) — USD→EUR daily reference rate{Colors.RESET}")
    print(f"{Colors.DIM}  FX@Closed   EUR per 1 USD on the Closed Date  → applied to Proceeds{Colors.RESET}")
    print(f"{Colors.DIM}              (converts what you received in USD into EUR at the day-of-sale rate){Colors.RESET}")
    print(f"{Colors.DIM}  FX@Opened   EUR per 1 USD on the Opened Date  → applied to Cost Basis{Colors.RESET}")
    print(f"{Colors.DIM}              (converts your original purchase cost into EUR at the day-of-acquisition rate){Colors.RESET}")
    print(f"{Colors.DIM}  Note: EUR Gain% may differ from USD Gain% due to exchange rate movement between dates.{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * TABLE_WIDTH}{Colors.RESET}")
    print()


# Total visual line width:
# 2 (indent) + 10 + 2 + 10 + 2 + 4 + 2 + 12 + 2 + 12 + 2 + 12 + 2 + 8
#   + 2 + 10 + 2 + 10 + 2 + 12 + 2 + 12 + 2 + 12 + 2 + 8 = 158
TABLE_WIDTH = 158


def print_section(title: str):
    """Print a section header."""
    print(f"\n{Colors.BOLD}{Colors.BLUE}┌{'─' * (TABLE_WIDTH - 2)}┐{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}│ {title:<{TABLE_WIDTH - 4}} │{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}└{'─' * (TABLE_WIDTH - 2)}┘{Colors.RESET}")


def print_table_header():
    """Print the transaction table header."""
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
    print(f"  {Colors.DIM}* EUR Gain is FX-adjusted (Proceeds converted at Closed Date rate, Cost Basis at Opened Date rate){Colors.RESET}")


def main():
    parser = argparse.ArgumentParser(
        description="Calculate Irish CGT from Schwab Realized Gain/Loss CSV"
    )
    parser.add_argument("csv_file", help="Path to Schwab Realized Gain/Loss CSV file")
    args = parser.parse_args()

    # Read and parse CSV
    transactions = []
    with open(args.csv_file, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = list(reader)

    # Find header row
    header_row = None
    for i, row in enumerate(rows):
        if row and row[0] == "Symbol":
            header_row = i
            break

    if header_row is None:
        print(f"{Colors.RED}Error: Could not find header row in CSV{Colors.RESET}")
        sys.exit(1)

    headers = rows[header_row]
    data_rows = rows[header_row + 1:]

    # Parse transactions
    for row in data_rows:
        if not row or not row[0].strip():
            continue
        try:
            txn = {
                "symbol": row[0].strip(),
                "name": row[1].strip(),
                "closed_date": parse_date(row[2]),
                "opened_date": parse_date(row[3]),
                "quantity": int(row[4]),
                "proceeds_per_share": parse_currency(row[5]),
                "cost_per_share": parse_currency(row[6]),
                "proceeds_usd": parse_currency(row[7]),
                "cost_basis_usd": parse_currency(row[8]),
                "gain_loss_usd": parse_currency(row[9]),
                "gain_loss_pct": parse_percentage(row[10]),
                "term": row[13].strip(),
            }
            transactions.append(txn)
        except (ValueError, IndexError):
            continue

    if not transactions:
        print(f"{Colors.RED}Error: No valid transactions found in CSV{Colors.RESET}")
        sys.exit(1)

    print_header()

    # Group by symbol
    symbols = {}
    for txn in transactions:
        symbols.setdefault(txn["symbol"], []).append(txn)

    # Cache FX rates to avoid repeat API calls for same date
    fx_cache = {}

    def get_cached_fx(date: datetime) -> float:
        key = date.strftime("%Y-%m-%d")
        if key not in fx_cache:
            fx_cache[key] = get_ecb_fx_rate(date)
        return fx_cache[key]

    # Collect all unique dates first to batch-inform the user
    all_dates = set()
    for txn in transactions:
        all_dates.add(txn["opened_date"].strftime("%Y-%m-%d"))
        all_dates.add(txn["closed_date"].strftime("%Y-%m-%d"))

    print(f"  {Colors.DIM}Fetching FX rates from ECB for {len(all_dates)} unique dates...{Colors.RESET}")

    total_proceeds_eur = 0.0
    total_cost_eur = 0.0
    total_gain_eur = 0.0
    total_proceeds_usd = 0.0
    total_cost_usd = 0.0
    total_gain_usd = 0.0

    for symbol, txns in symbols.items():
        print_section(f"Symbol: {symbol} — {txns[0]['name']}")
        print_table_header()

        symbol_proceeds_eur = 0.0
        symbol_cost_eur = 0.0
        symbol_gain_eur = 0.0

        symbol_gain_usd = 0.0
        symbol_proceeds_usd = 0.0
        symbol_cost_usd = 0.0

        for txn in txns:
            # Get FX rates: EUR per 1 USD at respective dates
            fx_at_closed = get_cached_fx(txn["closed_date"])   # for Proceeds
            fx_at_opened = get_cached_fx(txn["opened_date"])   # for Cost Basis

            # Convert to EUR using date-specific FX rates
            proceeds_eur = txn["proceeds_usd"] * fx_at_closed
            cost_eur = txn["cost_basis_usd"] * fx_at_opened
            gain_eur = proceeds_eur - cost_eur
            gain_pct_eur = (gain_eur / cost_eur * 100) if cost_eur != 0 else 0.0

            # USD gain (straight from data)
            gain_usd = txn["gain_loss_usd"]
            gain_pct_usd = txn["gain_loss_pct"]

            symbol_proceeds_eur += proceeds_eur
            symbol_cost_eur += cost_eur
            symbol_gain_eur += gain_eur
            symbol_gain_usd += gain_usd
            symbol_proceeds_usd += txn["proceeds_usd"]
            symbol_cost_usd += txn["cost_basis_usd"]

            # Color based on gain percentages
            color_eur = color_gain_loss(gain_pct_eur)
            color_usd = color_gain_loss(gain_pct_usd)

            # Pre-format colored cells to exact width so ANSI codes don't affect alignment
            sep = "  "
            gain_usd_str     = fmt_money(gain_usd, '$', 12)
            gain_pct_usd_str = fmt_pct(gain_pct_usd)
            gain_eur_str     = fmt_money(gain_eur, '€', 12)
            gain_pct_eur_str = fmt_pct(gain_pct_eur)

            row_str = (
                f"  {txn['closed_date'].strftime('%m/%d/%Y'):<10}{sep}"
                f"{txn['opened_date'].strftime('%m/%d/%Y'):<10}{sep}"
                f"{txn['quantity']:>4}{sep}"
                f"{fmt_money(txn['proceeds_usd'], '$', 12)}{sep}"
                f"{fmt_money(txn['cost_basis_usd'], '$', 12)}{sep}"
                f"{color_usd}{gain_usd_str}{Colors.RESET}{sep}"
                f"{color_usd}{gain_pct_usd_str}{Colors.RESET}{sep}"
                f"{fx_at_closed:>10.6f}{sep}"
                f"{fx_at_opened:>10.6f}{sep}"
                f"{fmt_money(proceeds_eur, '€', 12)}{sep}"
                f"{fmt_money(cost_eur, '€', 12)}{sep}"
                f"{color_eur}{gain_eur_str}{Colors.RESET}{sep}"
                f"{color_eur}{gain_pct_eur_str}{Colors.RESET}"
            )
            print(row_str)

        # Symbol subtotal
        sym_gain_pct_eur = (symbol_gain_eur / symbol_cost_eur * 100) if symbol_cost_eur != 0 else 0
        sym_gain_pct_usd = (symbol_gain_usd / symbol_cost_usd * 100) if symbol_cost_usd != 0 else 0
        color_eur = color_gain_loss(sym_gain_pct_eur)
        color_usd = color_gain_loss(sym_gain_pct_usd)

        sep = "  "
        sym_gain_usd_str = fmt_money(symbol_gain_usd, '$', 12)
        sym_pct_usd_str  = fmt_pct(sym_gain_pct_usd)
        sym_gain_eur_str = fmt_money(symbol_gain_eur, '€', 12)
        sym_pct_eur_str  = fmt_pct(sym_gain_pct_eur)

        print(f"  {Colors.DIM}{'─' * (TABLE_WIDTH - 2)}{Colors.RESET}")
        print(
            f"  {Colors.BOLD}{'SUBTOTAL':<10}{Colors.RESET}{sep}"
            f"{'':>10}{sep}"
            f"{'':>4}{sep}"
            f"{fmt_money(symbol_proceeds_usd, '$', 12)}{sep}"
            f"{fmt_money(symbol_cost_usd, '$', 12)}{sep}"
            f"{color_usd}{sym_gain_usd_str}{Colors.RESET}{sep}"
            f"{color_usd}{sym_pct_usd_str}{Colors.RESET}{sep}"
            f"{'':>10}{sep}"
            f"{'':>10}{sep}"
            f"{fmt_money(symbol_proceeds_eur, '€', 12)}{sep}"
            f"{fmt_money(symbol_cost_eur, '€', 12)}{sep}"
            f"{color_eur}{sym_gain_eur_str}{Colors.RESET}{sep}"
            f"{color_eur}{sym_pct_eur_str}{Colors.RESET}"
        )

        total_proceeds_eur += symbol_proceeds_eur
        total_cost_eur += symbol_cost_eur
        total_gain_eur += symbol_gain_eur
        total_proceeds_usd += symbol_proceeds_usd
        total_cost_usd += symbol_cost_usd
        total_gain_usd += symbol_gain_usd

    # ═══════════════════════════════════════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════════════════════════════════════
    print(f"\n\n{Colors.BOLD}{Colors.CYAN}{'═' * TABLE_WIDTH}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}  CGT SUMMARY{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * TABLE_WIDTH}{Colors.RESET}\n")

    # Calculate taxable amount
    if total_gain_eur > 0:
        taxable_gain = max(0, total_gain_eur - ANNUAL_EXEMPTION)
    else:
        taxable_gain = 0.0

    cgt_payable = taxable_gain * CGT_RATE
    net_proceeds = total_proceeds_eur - cgt_payable

    label_width = 40
    value_width = 15

    print(f"  {Colors.BOLD}{'Total Proceeds (EUR):':<{label_width}}{Colors.RESET}"
          f"{Colors.WHITE}€{total_proceeds_eur:>{value_width},.2f}{Colors.RESET}")
    print(f"  {Colors.BOLD}{'Total Cost Basis (EUR):':<{label_width}}{Colors.RESET}"
          f"{Colors.WHITE}€{total_cost_eur:>{value_width},.2f}{Colors.RESET}")

    gain_color = Colors.GREEN if total_gain_eur >= 0 else Colors.RED
    print(f"  {Colors.BOLD}{'Total Gain/Loss (EUR):':<{label_width}}{Colors.RESET}"
          f"{gain_color}€{total_gain_eur:>{value_width},.2f}{Colors.RESET}")

    print(f"\n  {Colors.DIM}{'─' * 60}{Colors.RESET}")

    print(f"  {Colors.BOLD}{'Annual Exemption:':<{label_width}}{Colors.RESET}"
          f"{Colors.WHITE}€{ANNUAL_EXEMPTION:>{value_width},.2f}{Colors.RESET}")
    print(f"  {Colors.BOLD}{'Taxable Gain (after exemption):':<{label_width}}{Colors.RESET}"
          f"{Colors.WHITE}€{taxable_gain:>{value_width},.2f}{Colors.RESET}")

    print(f"\n  {Colors.DIM}{'─' * 60}{Colors.RESET}")

    print(f"  {Colors.BOLD}{Colors.RED}{'CGT Payable (' + f'{CGT_RATE*100:.0f}%' + '):':<{label_width}}{Colors.RESET}"
          f"{Colors.RED}€{cgt_payable:>{value_width},.2f}{Colors.RESET}")

    print(f"\n  {Colors.DIM}{'─' * 60}{Colors.RESET}")
    print(f"  {Colors.DIM}Payment & filing deadlines (Irish CGT):{Colors.RESET}")
    print(f"  {Colors.DIM}  • Disposals Jan–Nov  → pay by 15 December of the same year{Colors.RESET}")
    print(f"  {Colors.DIM}  • Disposals in Dec   → pay by 31 January of the following year{Colors.RESET}")
    print(f"  {Colors.DIM}  • Tax return          → file by 31 October of the following year{Colors.RESET}")
    print(f"  {Colors.DIM}  • Must file even if no tax is due{Colors.RESET}")
    print(f"  {Colors.DIM}  • Pay via Revenue Online Service (ROS) or myAccount{Colors.RESET}")
    print(f"  {Colors.DIM}  ↳ Full details: https://www.citizensinformation.ie/en/money-and-tax/tax/capital-taxes/capital-gains-tax/#6acae0{Colors.RESET}")

    print(f"\n  {Colors.DIM}{'─' * 60}{Colors.RESET}")

    print(f"  {Colors.BOLD}{Colors.GREEN}{'Net Amount After Tax (EUR):':<{label_width}}{Colors.RESET}"
          f"{Colors.GREEN}€{net_proceeds:>{value_width},.2f}{Colors.RESET}")

    print(f"\n{Colors.BOLD}{Colors.CYAN}{'═' * TABLE_WIDTH}{Colors.RESET}\n")

    # Legend
    print(f"  {Colors.DIM}Legend:{Colors.RESET}")
    print(f"    {Colors.LIGHT_PURPLE}■{Colors.RESET} Gain > 20%    "
          f"{Colors.GREEN}■{Colors.RESET} Gain 0-20%    "
          f"{Colors.YELLOW}■{Colors.RESET} Loss 0-10%    "
          f"{Colors.RED}■{Colors.RESET} Loss > 10%")
    print()


if __name__ == "__main__":
    main()
