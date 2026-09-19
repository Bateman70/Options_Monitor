import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import os
import re
import requests
from tastytrade import Account, Session
from tastytrade.instruments import Equity

# ==========================================
# 1. CREDENTIALS & CONFIG
# ==========================================
CLIENT_SECRET = os.environ.get("TASTY_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.environ.get("TASTY_REFRESH_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Thresholds
PROFIT_TARGET_PCT = float(os.environ.get("PROFIT_TARGET_PCT", "0.50"))      # +50% profit target
DTE_DEFENSE_THRESHOLD = int(os.environ.get("DTE_DEFENSE_THRESHOLD", "21"))  # 21 DTE management
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "2.00"))             # Alert at 2x credit loss (-200%)
STRIKE_TEST_PCT = float(os.environ.get("STRIKE_TEST_PCT", "0.02"))          # Alert when spot is within 2% of short strike


# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")


def parse_occ_symbol(symbol: str):
    """
    Parses OCC symbols (e.g. 'SPY   261016P00560000') into:
    (underlying, expiration_date, option_type, strike_price)
    """
    match = re.match(r"^([A-Z\s]+?)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", symbol)
    if not match:
        return None
    underlying = match.group(1).strip()
    year = int("20" + match.group(2))
    month = int(match.group(3))
    day = int(match.group(4))
    exp_date = datetime(year, month, day).date()
    opt_type = "Call" if match.group(5) == "C" else "Put"
    strike = float(match.group(6)) / 1000.0
    return underlying, exp_date, opt_type, strike


async def get_underlying_prices(session: Session, symbols: set) -> dict:
    """Fetches approximate current market prices for underlying symbols."""
    prices = {}
    for sym in symbols:
        try:
            equity = await Equity.get_equity(session, sym)
            # Fetch market quote/closing price
            if equity:
                prices[sym] = float(equity.last_price or 0.0)
        except Exception:
            prices[sym] = None
    return prices


# ==========================================
# 3. CORE MONITORING & SPREAD GROUPING
# ==========================================
async def run_monitor():
    print("Connecting to Tastytrade...")
    session = Session(CLIENT_SECRET, REFRESH_TOKEN)

    accounts = await Account.get(session)
    account = accounts[0]
    positions = await account.get_positions(session)

    option_positions = [p for p in positions if p.instrument_type == "Equity Option"]
    if not option_positions:
        print(f"Connected to {account.account_number}: No open options positions found.")
        return

    print(f"Connected to {account.account_number}: Evaluating {len(option_positions)} option leg(s)...")

    # Group positions by (Underlying, Expiration Date)
    grouped = defaultdict(list)
    underlyings = set()
    for p in option_positions:
        parsed = parse_occ_symbol(p.symbol)
        if parsed:
            underlying, exp_date, opt_type, strike = parsed
            underlyings.add(underlying)
            grouped[(underlying, exp_date)].append({
                "pos": p,
                "type": opt_type,
                "strike": strike,
                "qty": int(p.quantity),
                "open_price": float(p.average_open_price),
                "mark_price": float(p.close_price),
                "side": p.quantity_direction  # 'Long' or 'Short'
            })

    # Fetch underlying spot prices
    spot_prices = await get_underlying_prices(session, underlyings)

    alerts = []
    today = datetime.now(timezone.utc).date()

    for (underlying, exp_date), legs in grouped.items():
        dte = (exp_date - today).days
        spot = spot_prices.get(underlying)
        spot_str = f"${spot:.2f}" if spot else "N/A"

        total_legs = len(legs)
        strategy = "Option Position"
        if total_legs == 1:
            strategy = f"Single {legs[0]['side']} {legs[0]['type']}"
        elif total_legs == 2:
            types = {leg["type"] for leg in legs}
            sides = {leg["side"] for leg in legs}
            if len(types) == 1 and len(sides) == 2:
                short_leg = next(l for l in legs if l["side"] == "Short")
                strategy = f"Vertical {'Credit' if short_leg['open_price'] > 0 else 'Debit'} Spread"
            elif len(types) == 2 and len(sides) == 1:
                strategy = "Strangle" if sides == {"Short"} else "Long Strangle"
        elif total_legs == 4:
            strategy = "Iron Condor"

        net_open_credit = 0.0
        net_mark_cost = 0.0

        for leg in legs:
            multiplier = -1 if leg["side"] == "Short" else 1
            net_open_credit -= leg["open_price"] * multiplier
            net_mark_cost -= leg["mark_price"] * multiplier

        strikes_desc = "/".join(
            str(int(l["strike"]) if l["strike"].is_integer() else l["strike"])
            for l in sorted(legs, key=lambda x: x["strike"])
        )

        short_legs = [l for l in legs if l["side"] == "Short"]

        # Evaluate Credit Trades
        if net_open_credit > 0:
            profit_pct = (net_open_credit - net_mark_cost) / net_open_credit

            # ---------------------------------------------------------
            # 1. FANTASTIC: Take Profit (e.g. >= 50% max profit)
            # ---------------------------------------------------------
            if profit_pct >= PROFIT_TARGET_PCT:
                alerts.append(
                    f"🎯 *TAKE PROFIT: {underlying} {strategy}*\n"
                    f"• Spot Price: `{spot_str}` | Strikes: `{strikes_desc}`\n"
                    f"• Expiry: `{exp_date}` ({dte} DTE)\n"
                    f"• Realized Gain: *{profit_pct * 100:.1f}%* (Target: {PROFIT_TARGET_PCT * 100:.0f}%)\n"
                    f"• Open Credit: ${net_open_credit:.2f} \vert{} Current Mark:${net_mark_cost:.2f}"
                )

            # ---------------------------------------------------------
            # 2. EMERGENCY: Stop-Loss Trigger (e.g. loss >= 200% of credit)
            # ---------------------------------------------------------
            elif profit_pct <= -STOP_LOSS_PCT:
                alerts.append(
                    f"🛑 *STOP-LOSS THRESHOLD: {underlying} {strategy}*\n"
                    f"• Spot Price: `{spot_str}` | Strikes: `{strikes_desc}`\n"
                    f"• Expiry: `{exp_date}` ({dte} DTE)\n"
                    f"• Drawdown: *{profit_pct * 100:.1f}%* (Limit: -{STOP_LOSS_PCT * 100:.0f}%)\n"
                    f"• Open Credit: ${net_open_credit:.2f} \vert{} Current Mark:${net_mark_cost:.2f}\n"
                    f"• *Action:* Max loss limit breached — evaluate closing or hedging."
                )

            # ---------------------------------------------------------
            # 3. DEFENSE: 21 DTE Management Rule
            # ---------------------------------------------------------
            elif dte <= DTE_DEFENSE_THRESHOLD:
                alerts.append(
                    f"⏳ *TIME DEFENSE: {underlying} {strategy}*\n"
                    f"• Spot Price: `{spot_str}` | Strikes: `{strikes_desc}`\n"
                    f"• Expiry: `{exp_date}` (*{dte} DTE remaining*)\n"
                    f"• Current P/L: *{profit_pct * 100:.1f}%*\n"
                    f"• *Action:* 21 DTE threshold hit — consider rolling untested side or managing risk."
                )

            # ---------------------------------------------------------
            # 4. PROXIMITY: Short Strike Test Check
            # ---------------------------------------------------------
            if spot and short_legs:
                for s_leg in short_legs:
                    strike = s_leg["strike"]
                    opt_type = s_leg["type"]

                    # Distance from spot to short strike
                    distance_pct = (spot - strike) / strike if opt_type == "Call" else (strike - spot) / strike

                    # If spot is within buffer (or has breached the strike)
                    if distance_pct >= -STRIKE_TEST_PCT:
                        status = "🔥 *IN THE MONEY*" if distance_pct >= 0 else "⚠️ *PROXIMITY WARNING*"
                        alerts.append(
                            f"{status}: *{underlying} Short {opt_type} Tested*\n"
                            f"• Spot: `{spot_str}` vs Short Strike: `${strike:.2f}`\n"
                            f"• Buffer: *{abs(distance_pct) * 100:.2f}%* away (Threshold: {STRIKE_TEST_PCT * 100:.1f}%)\n"
                            f"• Expiry: `{exp_date}` ({dte} DTE)\n"
                            f"• *Action:* Short leg under pressure. Prepare to roll untested side or adjust."
                        )

    # Dispatch alerts if any conditions were met
    if alerts:
        header = "🚨 *Tastytrade Options Alert*\n\n"
        send_telegram(header + "\n\n---\n\n".join(alerts))
        print(f"Triggered and sent {len(alerts)} alert(s) to Telegram.")
    else:
        print("All spreads and positions checked. No alerts triggered.")


if __name__ == "__main__":
    asyncio.run(run_monitor())