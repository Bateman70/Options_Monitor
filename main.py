import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import os
import re
import requests
from tastytrade import Account, Session

# ==========================================
# 1. CREDENTIALS & CONFIG
# ==========================================
CLIENT_SECRET = os.environ.get("TASTY_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.environ.get("TASTY_REFRESH_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

PROFIT_TARGET_PCT = float(os.environ.get("PROFIT_TARGET_PCT", "0.50"))
DTE_DEFENSE_THRESHOLD = int(os.environ.get("DTE_DEFENSE_THRESHOLD", "21"))


# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================
def send_telegram(text: str):
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": text,
      "parse_mode": "Markdown",
  }
  try:
    response = requests.post(url, json=payload)
    return response.json()
  except Exception as e:
    print(f"Failed to send Telegram message: {e}")


def parse_occ_symbol(symbol: str):
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


# ==========================================
# 3. CORE MONITORING & SPREAD GROUPING
# ==========================================
async def run_monitor():
  print("Connecting to Tastytrade...")
  session = Session(CLIENT_SECRET, REFRESH_TOKEN)

  # Handles async on Render Linux environment
  accounts = await Account.get(session)
  account = accounts[0]
  positions = await account.get_positions(session)

  # Filter equity options only
  option_positions = [
      p for p in positions if p.instrument_type == "Equity Option"
  ]
  if not option_positions:
    print(
        f"Connected to {account.account_number}: No open options positions"
        " found."
    )
    return

  print(
      f"Connected to {account.account_number}: Evaluating"
      f" {len(option_positions)} option leg(s)..."
  )

  grouped = defaultdict(list)
  for p in option_positions:
    parsed = parse_occ_symbol(p.symbol)
    if parsed:
      underlying, exp_date, opt_type, strike = parsed
      grouped[(underlying, exp_date)].append({
          "pos": p,
          "type": opt_type,
          "strike": strike,
          "qty": int(p.quantity),
          "open_price": float(p.average_open_price),
          "mark_price": float(p.close_price),
          "side": p.quantity_direction,
      })

  alerts = []
  today = datetime.now(timezone.utc).date()

  for (underlying, exp_date), legs in grouped.items():
    dte = (exp_date - today).days

    total_legs = len(legs)
    strategy = "Option Position"
    if total_legs == 1:
      strategy = f"Single {legs[0]['side']} {legs[0]['type']}"
    elif total_legs == 2:
      types = {leg["type"] for leg in legs}
      sides = {leg["side"] for leg in legs}
      if len(types) == 1 and len(sides) == 2:
        short_leg = next(l for l in legs if l["side"] == "Short")
        strategy = (
            "Vertical"
            f" {'Credit' if short_leg['open_price'] > 0 else 'Debit'} Spread"
        )
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

    if net_open_credit > 0:
      profit_pct = (net_open_credit - net_mark_cost) / net_open_credit

      if profit_pct >= PROFIT_TARGET_PCT:
        alerts.append(
            f"🎯 *TAKE PROFIT: {underlying} {strategy}*\n"
            f"• Strikes: `{strikes_desc}` | Expiry: `{exp_date}` ({dte} DTE)\n"
            f"• Realized Gain: *{profit_pct * 100:.1f}%* (Target:"
            f" {PROFIT_TARGET_PCT * 100:.0f}%)\n"
            f"• Open Credit: ${net_open_credit:.2f} | Current Mark:"
            f" ${net_mark_cost:.2f}"
        )
      elif dte <= DTE_DEFENSE_THRESHOLD:
        alerts.append(
            f"⏳ *TIME DEFENSE: {underlying} {strategy}*\n"
            f"• Strikes: `{strikes_desc}` | Expiry: `{exp_date}` (*{dte} DTE"
            " remaining*)\n"
            f"• Current Gain/Loss: *{profit_pct * 100:.1f}%*\n"
            "• Action: Consider rolling untested side or taking off risk."
        )

  if alerts:
    header = "🚨 *Tastytrade Options Alert*\n\n"
    send_telegram(header + "\n\n".join(alerts))
    print(f"Triggered and sent {len(alerts)} alert(s) to Telegram.")
  else:
    print("All spreads and positions checked. No alerts triggered.")


if __name__ == "__main__":
  asyncio.run(run_monitor())