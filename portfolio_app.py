"""
Portfolio Almanac — Streamlit Edition
──────────────────────────────────────
Open once, see your portfolio anywhere.

Your transactions live in a public raw URL (GitHub, Gist, S3, anywhere).
The app fetches the CSV, computes positions, pulls live prices, and renders
a dashboard. Feature parity with the HTML version:
  · Stocks (via yfinance), ETFs, REITs, InvITs — all NSE tickers
  · Mutual funds (via MFapi.in scheme codes)
  · Optional quantity OR amount per row (SIP-friendly)
  · Historical price lookup with VWAP-approx (H+L+C)/3 for stocks, NAV for MFs
  · Weekend/holiday fallback to nearest prior trading day

Configure once via the CSV_URL constant below, deploy to Streamlit Cloud, done.
"""

import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import requests
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date, timedelta, timedelta
import io
import warnings
warnings.filterwarnings("ignore")

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURATION — edit this one line to point at your CSV
# ═══════════════════════════════════════════════════════════════════════════

# Paste your CSV URL here. Examples:
#   GitHub raw:   https://raw.githubusercontent.com/USER/REPO/main/transactions.csv
#   Secret Gist:  https://gist.githubusercontent.com/USER/GISTID/raw/transactions.csv
#   S3 / Dropbox: any URL that returns CSV content
#
# If the URL is empty or fetches fail, the app falls back to the Streamlit
# file uploader so you can still use it.
CSV_URL = ""

# Optional: password-gate the app (Streamlit Cloud reads from st.secrets).
# To enable, set ENABLE_PASSWORD_GATE = True AND add to Streamlit Cloud:
#     Settings → Secrets → password = "yourchosenpassword"
ENABLE_PASSWORD_GATE = False

# ═══════════════════════════════════════════════════════════════════════════
# PAGE SETUP & THEME
# ═══════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Portfolio Almanac",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Custom CSS — editorial almanac aesthetic
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;0,9..144,600;1,9..144,400&family=JetBrains+Mono:wght@400;500&family=Inter:wght@400;500;600&display=swap');

  html, body, [class*="css"]  {
    font-family: 'Inter', sans-serif;
    color: #1C1B17;
  }
  .main .block-container {
    padding-top: 2rem; padding-bottom: 4rem; max-width: 1280px;
  }
  h1, h2, h3, .stMarkdown h1, .stMarkdown h2, .stMarkdown h3 {
    font-family: 'Fraunces', serif; font-weight: 500;
    letter-spacing: -0.01em;
  }
  .almanac-title {
    font-family: 'Fraunces', serif; font-size: 56px; line-height: 1;
    font-weight: 500; letter-spacing: -0.02em;
    border-bottom: 2px solid #1C1B17; padding-bottom: 16px; margin-bottom: 8px;
  }
  .almanac-title em { font-style: italic; color: #8B3A1F; font-weight: 400; }
  .eyebrow {
    font-family: 'JetBrains Mono', monospace; font-size: 10px;
    letter-spacing: 0.2em; text-transform: uppercase; color: #8A8576;
    margin-bottom: 8px;
  }
  .masthead-meta {
    font-family: 'JetBrains Mono', monospace; font-size: 11px;
    color: #8A8576; letter-spacing: 0.05em; margin-top: -4px;
    margin-bottom: 24px;
  }
  div[data-testid="stMetric"] {
    background: rgba(255,255,255,0.5); padding: 16px 20px;
    border: 1px solid #D9D3C0; border-radius: 2px;
  }
  div[data-testid="stMetricLabel"] {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px !important; letter-spacing: 0.15em;
    text-transform: uppercase; color: #8A8576 !important;
  }
  div[data-testid="stMetricValue"] {
    font-family: 'Fraunces', serif; font-weight: 400;
    font-size: 32px !important; letter-spacing: -0.02em;
  }
  section-divider {
    border-bottom: 1px solid #1C1B17; padding-bottom: 8px;
    margin: 24px 0 12px; font-family: 'Fraunces', serif;
    font-size: 24px; font-weight: 500;
  }
  code { background: rgba(28,27,23,0.06); padding: 1px 6px; border-radius: 2px; }
  .stDataFrame { font-family: 'Inter', sans-serif; }
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# OPTIONAL PASSWORD GATE
# ═══════════════════════════════════════════════════════════════════════════

def check_password():
    if not ENABLE_PASSWORD_GATE:
        return True
    expected = st.secrets.get("password") if "password" in st.secrets else None
    if not expected:
        st.warning("Password gate is enabled but no password is set in Streamlit secrets. Disabling gate.")
        return True
    if st.session_state.get("authenticated"):
        return True
    pw = st.text_input("Password", type="password")
    if pw == expected:
        st.session_state["authenticated"] = True
        st.rerun()
    elif pw:
        st.error("Incorrect password.")
    return False

if not check_password():
    st.stop()

# ═══════════════════════════════════════════════════════════════════════════
# TYPE ALIASES — map user-provided type values → internal pipeline
# ═══════════════════════════════════════════════════════════════════════════

TYPE_ALIASES = {
    "stock":       ("stock", "Stock"),
    "stocks":      ("stock", "Stock"),
    "equity":      ("stock", "Stock"),
    "share":       ("stock", "Stock"),
    "shares":      ("stock", "Stock"),
    "etf":         ("stock", "ETF"),
    "reit":        ("stock", "REIT"),
    "invit":       ("stock", "InvIT"),
    "mf":          ("mf",    "MF"),
    "mutual fund": ("mf",    "MF"),
    "mutualfund":  ("mf",    "MF"),
    "fund":        ("mf",    "MF"),
}

# ═══════════════════════════════════════════════════════════════════════════
# CSV LOADING
# ═══════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300, show_spinner=False)
def fetch_csv_from_url(url: str) -> str:
    """Fetch CSV text from a public URL. Cached 5 min to avoid hammering."""
    resp = requests.get(url, timeout=10, headers={"User-Agent": "portfolio-almanac"})
    resp.raise_for_status()
    return resp.text


def load_transactions(csv_text: str) -> tuple[pd.DataFrame, list]:
    """Parse CSV and normalize. Returns (valid_txs_df, skipped_list)."""
    df = pd.read_csv(io.StringIO(csv_text), dtype=str).fillna("")
    # Normalize column names to lowercase
    df.columns = [c.strip().lower() for c in df.columns]

    required = {"date", "type", "symbol"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")

    skipped = []
    records = []

    for idx, row in df.iterrows():
        rownum = idx + 2  # +1 for 1-indexed, +1 for header
        type_raw = str(row.get("type", "")).lower().strip()
        symbol = str(row.get("symbol", "")).strip()
        name = str(row.get("name", symbol)).strip() or symbol
        action = str(row.get("action", "buy")).lower().strip() or "buy"
        date = str(row.get("date", "")).strip()

        if not symbol:
            skipped.append((rownum, "missing symbol"))
            continue
        if not date:
            skipped.append((rownum, "missing date"))
            continue

        type_info = TYPE_ALIASES.get(type_raw)
        if not type_info:
            skipped.append((rownum, f'unrecognized type "{row.get("type","")}" (use: stock, mf, etf, reit)'))
            continue
        kind, label = type_info

        # MF scheme code must be numeric
        if kind == "mf" and not symbol.isdigit():
            skipped.append((rownum, f'"{symbol}" is not a numeric AMFI scheme code. ISINs do not work — look up the code at mfapi.in'))
            continue

        # Parse optional numeric fields
        def _parsef(v):
            try:
                f = float(v)
                return f if f > 0 else None
            except (ValueError, TypeError):
                return None

        qty    = _parsef(row.get("quantity", ""))
        price  = _parsef(row.get("price", ""))
        amount = _parsef(row.get("amount", ""))

        if qty is not None and price is not None:
            mode = "complete"
        elif qty is not None:
            mode = "need_price"
        elif amount is not None:
            mode = "need_qty"
        else:
            skipped.append((rownum, "no quantity or amount provided"))
            continue

        records.append({
            "rownum": rownum, "type": kind, "display_type": label,
            "symbol": symbol, "name": name, "action": action, "date": date,
            "mode": mode, "qty": qty, "price": price, "amount": amount,
            "price_source": "user" if price is not None else "pending",
            "actual_date": None, "offset_days": 0,
        })

    return pd.DataFrame(records), skipped


# ═══════════════════════════════════════════════════════════════════════════
# PRICE FETCHING
# ═══════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300, show_spinner=False)
def fetch_stock_history(ticker: str, start: str = None) -> pd.DataFrame:
    """Fetch daily OHLC from yfinance. Cached 5 min per ticker."""
    tkr = yf.Ticker(ticker)
    if start:
        hist = tkr.history(start=start, auto_adjust=True)
    else:
        hist = tkr.history(period="5y", auto_adjust=True)
    if hist.empty:
        raise ValueError(f"No data for ticker {ticker}")
    hist.index = hist.index.tz_localize(None) if hist.index.tz else hist.index
    return hist


@st.cache_data(ttl=300, show_spinner=False)
def fetch_mf_history(scheme_code: str) -> list:
    """Fetch full NAV history from MFapi.in. Cached 5 min per scheme."""
    url = f"https://api.mfapi.in/mf/{scheme_code}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "SUCCESS" or not payload.get("data"):
        raise ValueError(f"No NAV data for scheme {scheme_code}")
    return payload["data"]


def get_stock_current_and_prev(ticker: str):
    """Latest close + previous close for today's P&L."""
    hist = fetch_stock_history(ticker)
    if len(hist) < 1:
        raise ValueError(f"Empty history for {ticker}")
    current = float(hist["Close"].iloc[-1])
    prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else current
    return current, prev


def get_stock_historical_price(ticker: str, target_date: str):
    """VWAP-approx price = (H+L+C)/3 on target_date. Walk back up to 10 days
    if target_date is a weekend/holiday/pre-listing."""
    hist = fetch_stock_history(ticker, start=(pd.Timestamp(target_date) - pd.Timedelta(days=30)).strftime("%Y-%m-%d"))
    target = pd.Timestamp(target_date).normalize()

    for offset in range(11):
        probe = target - pd.Timedelta(days=offset)
        try:
            row = hist.loc[probe]
            if pd.notna(row["High"]) and pd.notna(row["Low"]) and pd.notna(row["Close"]):
                vwap = (row["High"] + row["Low"] + row["Close"]) / 3
                return float(vwap), probe.strftime("%Y-%m-%d"), offset
        except KeyError:
            continue
    raise ValueError(f"No price for {ticker} on {target_date} or 10 days prior")


def get_mf_current_and_prev(scheme_code: str):
    data = fetch_mf_history(scheme_code)
    current = float(data[0]["nav"])
    prev = float(data[1]["nav"]) if len(data) > 1 else current
    return current, prev


def get_mf_historical_price(scheme_code: str, target_date: str):
    data = fetch_mf_history(scheme_code)
    # Build date → NAV lookup (MFapi uses DD-MM-YYYY)
    lookup = {}
    for entry in data:
        dd, mm, yyyy = entry["date"].split("-")
        lookup[f"{yyyy}-{mm}-{dd}"] = float(entry["nav"])

    target = pd.Timestamp(target_date).normalize()
    for offset in range(11):
        probe = (target - pd.Timedelta(days=offset)).strftime("%Y-%m-%d")
        if probe in lookup:
            return lookup[probe], probe, offset
    raise ValueError(f"No NAV for scheme {scheme_code} on {target_date} or 10 days prior")


# ═══════════════════════════════════════════════════════════════════════════
# POSITION AGGREGATION
# ═══════════════════════════════════════════════════════════════════════════

def aggregate_positions(txs_df: pd.DataFrame) -> pd.DataFrame:
    """Group transactions by (type, symbol) → positions with avg cost & qty."""
    groups = {}
    for _, t in txs_df.iterrows():
        if t["price"] is None or t["qty"] is None:
            continue
        key = (t["type"], t["symbol"])
        if key not in groups:
            groups[key] = {
                "type": t["type"], "display_type": t["display_type"],
                "symbol": t["symbol"], "name": t["name"],
                "qty": 0.0, "invested": 0.0, "tx": 0,
                "auto_priced": 0, "auto_priced_shifted": 0, "qty_derived": 0,
            }
        g = groups[key]
        if t["action"] == "sell":
            avg_cost = g["invested"] / g["qty"] if g["qty"] > 0 else t["price"]
            g["invested"] -= avg_cost * t["qty"]
            g["qty"] -= t["qty"]
        else:
            g["invested"] += t["qty"] * t["price"]
            g["qty"] += t["qty"]
        g["tx"] += 1
        if t["price_source"] == "historical":
            g["auto_priced"] += 1
            if t["offset_days"] > 0:
                g["auto_priced_shifted"] += 1
        if t.get("qty_derived"):
            g["qty_derived"] += 1

    positions = [g for g in groups.values() if g["qty"] > 0.0001]
    for p in positions:
        p["avg_cost"] = p["invested"] / p["qty"]
    return pd.DataFrame(positions)


# ═══════════════════════════════════════════════════════════════════════════
# RESOLVE HISTORICAL PRICES & DERIVE QUANTITIES
# ═══════════════════════════════════════════════════════════════════════════

def resolve_historical_prices(txs_df: pd.DataFrame, progress_cb=None) -> tuple[pd.DataFrame, list]:
    """For rows missing price, fetch historical. For rows missing qty (amount-only),
    derive qty = amount / price after price is resolved."""
    pending = txs_df[txs_df["price"].isna()].index.tolist()
    # In pandas, None becomes NaN — handle both
    pending = [i for i in txs_df.index if pd.isna(txs_df.at[i, "price"])]
    issues = []

    total = len(pending)
    for n, i in enumerate(pending):
        t = txs_df.loc[i]
        if progress_cb:
            progress_cb((n + 1) / total, f"Historical price {n+1}/{total}: {t['symbol']} on {t['date']}")
        try:
            if t["type"] == "stock":
                price, actual, offset = get_stock_historical_price(t["symbol"], t["date"])
            else:
                price, actual, offset = get_mf_historical_price(t["symbol"], t["date"])
            txs_df.at[i, "price"] = price
            txs_df.at[i, "actual_date"] = actual
            txs_df.at[i, "offset_days"] = offset
            txs_df.at[i, "price_source"] = "historical"
        except Exception as e:
            issues.append({"row": int(t["rownum"]), "name": t["name"], "symbol": t["symbol"], "date": t["date"], "error": str(e)})
            txs_df.at[i, "price_source"] = "failed"

    # Derive qty for need_qty rows
    for i in txs_df.index:
        if (txs_df.at[i, "mode"] == "need_qty"
                and pd.isna(txs_df.at[i, "qty"])
                and pd.notna(txs_df.at[i, "price"])
                and pd.notna(txs_df.at[i, "amount"])):
            txs_df.at[i, "qty"] = txs_df.at[i, "amount"] / txs_df.at[i, "price"]
            txs_df.at[i, "qty_derived"] = True

    return txs_df, issues


def attach_current_prices(positions_df: pd.DataFrame, progress_cb=None) -> tuple[pd.DataFrame, list]:
    """Fetch live price + previous close for each position."""
    issues = []
    current_prices = []
    prev_closes = []
    total = len(positions_df)

    for n, (_, p) in enumerate(positions_df.iterrows()):
        if progress_cb:
            progress_cb((n + 1) / total, f"Live price {n+1}/{total}: {p['symbol']}")
        try:
            if p["type"] == "stock":
                cur, prev = get_stock_current_and_prev(p["symbol"])
            else:
                cur, prev = get_mf_current_and_prev(p["symbol"])
        except Exception as e:
            issues.append({"name": p["name"], "symbol": p["symbol"], "error": str(e)})
            cur = p["avg_cost"]
            prev = p["avg_cost"]
        current_prices.append(cur)
        prev_closes.append(prev)

    positions_df = positions_df.copy()
    positions_df["current_price"] = current_prices
    positions_df["prev_close"] = prev_closes
    positions_df["current_value"] = positions_df["qty"] * positions_df["current_price"]
    positions_df["pnl"] = positions_df["current_value"] - positions_df["invested"]
    positions_df["pnl_pct"] = (positions_df["pnl"] / positions_df["invested"]) * 100
    positions_df["day_pnl"] = positions_df["qty"] * (positions_df["current_price"] - positions_df["prev_close"])
    return positions_df, issues


# ═══════════════════════════════════════════════════════════════════════════
# HISTORICAL TIMELINE
# ═══════════════════════════════════════════════════════════════════════════
# For every calendar day from the first transaction to today, compute:
#   · total portfolio value    (Σ qty_held × close_price for each position)
#   · total invested cost      (running sum of buys minus cost-basis of sells)
#   · cumulative P&L           (value − invested)
#   · per-holding breakdown    (same but un-aggregated, for the stacked view)
#
# Key detail: prices are forward-filled across weekends/holidays so the line
# chart has no gaps. We replay every transaction in chronological order to
# get the exact qty held on each day — this handles partial sells correctly.

@st.cache_data(ttl=300, show_spinner=False)
def build_historical_timeline(txs_df_json: str, positions_json: str) -> pd.DataFrame:
    """Accepts JSON-serialized DataFrames (for Streamlit cache hashing).
    Returns long-format DataFrame with columns:
        date, symbol, name, value, invested
    Aggregated totals come from groupby(date).sum()."""
    txs_df   = pd.read_json(io.StringIO(txs_df_json), convert_dates=["date"])
    positions = pd.read_json(io.StringIO(positions_json))
    if txs_df.empty or positions.empty:
        return pd.DataFrame()

    txs_df = txs_df.copy()
    txs_df["date"] = pd.to_datetime(txs_df["date"])

    first_day = txs_df["date"].min().date()
    last_day  = date.today()
    full_idx  = pd.date_range(start=first_day, end=last_day, freq="D")

    all_frames = []
    for _, p in positions.iterrows():
        symbol = p["symbol"]
        kind   = p["type"]
        name   = p["name"]

        # ── Fetch daily close price series ──────────────────────────
        try:
            if kind == "stock":
                hist = fetch_stock_history(
                    symbol,
                    start=(pd.Timestamp(first_day) - pd.Timedelta(days=7)).strftime("%Y-%m-%d"),
                )
                prices = hist["Close"].rename("price")
            else:
                # fetch_mf_history returns a list of {"date": "DD-MM-YYYY", "nav": "123.45"}
                mf_raw = fetch_mf_history(symbol)
                mf_rows = {}
                for entry in mf_raw:
                    dd, mm, yyyy = entry["date"].split("-")
                    mf_rows[f"{yyyy}-{mm}-{dd}"] = float(entry["nav"])
                prices = pd.Series(mf_rows, name="price")
                prices.index = pd.to_datetime(prices.index)
        except Exception:
            continue  # skip if no history (delisted, wrong symbol etc.)

        prices.index = pd.to_datetime(prices.index).tz_localize(None) \
            if prices.index.tz else pd.to_datetime(prices.index)

        # Forward-fill weekends + holidays so every calendar day has a price
        prices = prices.reindex(full_idx, method="ffill")

        # ── Replay transactions → running qty + invested per day ────
        sym_txs = txs_df[
            txs_df["symbol"] == symbol
        ].sort_values("date").copy()

        running_qty      = 0.0
        running_invested = 0.0
        qty_series = pd.Series(0.0, index=full_idx, dtype=float)
        inv_series = pd.Series(0.0, index=full_idx, dtype=float)

        for _, t in sym_txs.iterrows():
            if pd.isna(t.get("quantity")) or pd.isna(t.get("price")):
                continue
            qty   = float(t["quantity"])
            price = float(t["price"])
            action = str(t.get("action", "buy")).lower()

            if action == "sell":
                avg_cost = running_invested / running_qty if running_qty > 0 else price
                running_invested -= avg_cost * qty
                running_qty      -= qty
            else:
                running_invested += qty * price
                running_qty      += qty

            # From this date forward, the series carries the new balances
            mask = full_idx >= t["date"].normalize()
            qty_series.loc[mask] = max(running_qty, 0.0)
            inv_series.loc[mask] = max(running_invested, 0.0)

        value_series = qty_series * prices.values

        frame = pd.DataFrame({
            "date":     full_idx,
            "symbol":   symbol,
            "name":     name,
            "qty":      qty_series.values,
            "invested": inv_series.values,
            "value":    value_series.values,
        })
        # Only keep days where the position actually existed
        frame = frame[frame["qty"] > 0]
        all_frames.append(frame)

    if not all_frames:
        return pd.DataFrame()

    return pd.concat(all_frames, ignore_index=True)


# ═══════════════════════════════════════════════════════════════════════════
# FORMATTING
# ═══════════════════════════════════════════════════════════════════════════

def fmt_inr(val):
    if pd.isna(val): return "—"
    abs_val = abs(val)
    sign = "-" if val < 0 else ""
    if abs_val >= 1e7:
        return f"{sign}₹{abs_val/1e7:.2f} Cr"
    if abs_val >= 1e5:
        return f"{sign}₹{abs_val/1e5:.2f} L"
    return f"{sign}₹{abs_val:,.2f}"


def fmt_pct(val):
    if pd.isna(val): return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"


# ═══════════════════════════════════════════════════════════════════════════
# MAIN APP
# ═══════════════════════════════════════════════════════════════════════════

# Masthead
col_a, col_b = st.columns([3, 1])
with col_a:
    st.markdown('<div class="eyebrow">Vol. I &nbsp;·&nbsp; Personal Securities Ledger</div>', unsafe_allow_html=True)
    st.markdown('<div class="almanac-title">The Portfolio <em>Almanac</em></div>', unsafe_allow_html=True)
with col_b:
    st.markdown(
        f'<div class="masthead-meta" style="text-align:right; margin-top:40px;">'
        f'{datetime.now().strftime("%A, %d %B %Y")}<br>'
        f'Stocks via yfinance · MFs via MFapi.in</div>',
        unsafe_allow_html=True,
    )

# Sidebar controls
with st.sidebar:
    st.header("Source")
    source_mode = st.radio(
        "CSV source",
        ["Hardcoded URL", "Paste URL", "Upload file"],
        index=0 if CSV_URL else 1,
        label_visibility="collapsed",
    )

    csv_url_to_use = None
    uploaded_file = None

    if source_mode == "Hardcoded URL":
        if not CSV_URL:
            st.warning("No CSV_URL is hardcoded. Edit `portfolio_app.py` or pick another source.")
        else:
            st.code(CSV_URL, language=None)
            csv_url_to_use = CSV_URL
    elif source_mode == "Paste URL":
        csv_url_to_use = st.text_input("Raw CSV URL", placeholder="https://raw.githubusercontent.com/...")
    else:
        uploaded_file = st.file_uploader("transactions.csv", type=["csv"])

    st.divider()
    if st.button("🔄 Refresh all prices", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption("Prices cached 5 min. Click to force refresh.")


# Load CSV
csv_text = None
source_label = None

if csv_url_to_use:
    try:
        with st.spinner("Fetching CSV from URL…"):
            csv_text = fetch_csv_from_url(csv_url_to_use)
        source_label = csv_url_to_use
    except Exception as e:
        st.error(f"Could not fetch CSV from URL: {e}")
        st.info("Try uploading a file instead, or check the URL.")
elif uploaded_file is not None:
    csv_text = uploaded_file.getvalue().decode("utf-8")
    source_label = uploaded_file.name

if csv_text is None:
    st.info("👈 Configure your CSV source in the sidebar to begin.")
    st.markdown("""
    ### First time?
    1. Put your `transactions.csv` in a **public** GitHub repo or secret gist.
    2. Copy the **raw** URL.
    3. Either paste it in the sidebar, or hardcode it in `portfolio_app.py` at the `CSV_URL` constant.

    ### CSV format
    ```
    date,type,name,symbol,action,quantity,price,amount
    2023-03-15,stock,Reliance Industries,RELIANCE.NS,buy,5,,
    2024-01-18,stock,HDFC Bank,HDFCBANK.NS,buy,5,1505.00,
    2023-07-01,mf,Mirae Large Cap,118989,buy,,,5000
    ```

    - `type`: `stock`, `mf`, `etf`, `reit`, `invit`
    - `symbol`: yfinance ticker (e.g. `RELIANCE.NS`) for stocks/ETFs/REITs; numeric AMFI scheme code for MFs
    - Provide at least one of `quantity` or `amount`
    - Blank price → looked up historically (VWAP-approx H+L+C ÷ 3 for stocks, NAV for MFs)
    """)
    st.stop()

# Parse transactions
try:
    txs_df, skipped = load_transactions(csv_text)
except Exception as e:
    st.error(f"CSV parse error: {e}")
    st.stop()

if txs_df.empty:
    st.error("No valid transactions found after parsing.")
    if skipped:
        with st.expander(f"⚠ {len(skipped)} rows skipped — see reasons"):
            for r, reason in skipped:
                st.markdown(f"- **Row {r}**: {reason}")
    st.stop()

# Resolve historical prices
progress = st.progress(0.0, text="Resolving historical prices…")
def _cb(frac, msg): progress.progress(frac, text=msg)
txs_df, history_issues = resolve_historical_prices(txs_df, progress_cb=_cb)
progress.empty()

# Aggregate positions
positions = aggregate_positions(txs_df)
if positions.empty:
    st.error("No valid positions could be built.")
    st.stop()

# Fetch live prices
progress = st.progress(0.0, text="Fetching live prices…")
positions, live_issues = attach_current_prices(positions, progress_cb=_cb)
progress.empty()

# ─────────────────────────────────────────────────────────────────────────
# KPI STRIP
# ─────────────────────────────────────────────────────────────────────────

total_invested = positions["invested"].sum()
total_current  = positions["current_value"].sum()
total_pnl      = total_current - total_invested
total_pnl_pct  = (total_pnl / total_invested * 100) if total_invested > 0 else 0
total_day_pnl  = positions["day_pnl"].sum()

k1, k2, k3, k4 = st.columns(4)
k1.metric("Total Invested", fmt_inr(total_invested))
k2.metric("Current Value",  fmt_inr(total_current))
k3.metric(
    "Total P&L",
    fmt_inr(total_pnl),
    delta=fmt_pct(total_pnl_pct),
)
k4.metric(
    "Today's P&L",
    fmt_inr(total_day_pnl),
    delta=f"{'▲' if total_day_pnl >= 0 else '▼'} vs prev close",
    delta_color="normal" if total_day_pnl >= 0 else "inverse",
)

# Status bar
status_bits = []
auto_priced = int(txs_df["price_source"].eq("historical").sum())
qty_derived = int(txs_df.get("qty_derived", pd.Series(False)).fillna(False).sum())
if not history_issues and not live_issues and not skipped:
    status_bits.append("🟢 All clean")
else:
    status_bits.append(f"🟡 {len(history_issues) + len(live_issues) + len(skipped)} issue(s)")
status_bits.append(f"{len(positions)} positions")
if auto_priced: status_bits.append(f"{auto_priced} auto-priced (VWAP H+L+C÷3)")
if qty_derived: status_bits.append(f"{qty_derived} qty-derived (amount÷price)")
st.caption(" · ".join(status_bits) + f" · source: {source_label[:60]}")

# Issues panel
if skipped or history_issues or live_issues:
    with st.expander("⚠ Issues", expanded=False):
        if skipped:
            st.markdown(f"**{len(skipped)} CSV rows skipped:**")
            for r, reason in skipped:
                st.markdown(f"- Row {r}: {reason}")
        if history_issues:
            st.markdown(f"**{len(history_issues)} historical price lookups failed:**")
            for h in history_issues:
                st.markdown(f"- Row {h['row']}: {h['name']} ({h['symbol']}) on {h['date']} — {h['error']}")
        if live_issues:
            st.markdown(f"**{len(live_issues)} live price fetches failed:**")
            for h in live_issues:
                st.markdown(f"- {h['name']} ({h['symbol']}) — {h['error']}")

# ─────────────────────────────────────────────────────────────────────────
# HOLDINGS TABLE
# ─────────────────────────────────────────────────────────────────────────

st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)
st.markdown("## Holdings *ledger*")

sorted_pos = positions.sort_values("current_value", ascending=False).copy()
sorted_pos["Display Name"] = sorted_pos.apply(
    lambda r: r["name"] + (f"  ⌁{r['auto_priced']}/{r['tx']}" if r["auto_priced"] > 0 else "")
                         + (f"  ∑{r['qty_derived']}/{r['tx']}" if r["qty_derived"] > 0 else ""),
    axis=1,
)

display_df = pd.DataFrame({
    "Type":      sorted_pos["display_type"],
    "Holding":   sorted_pos["Display Name"],
    "Symbol":    sorted_pos["symbol"],
    "Qty":       sorted_pos["qty"].map(lambda v: f"{v:,.3f}".rstrip("0").rstrip(".")),
    "Avg Cost":  sorted_pos["avg_cost"].map(lambda v: f"₹{v:,.2f}"),
    "Current":   sorted_pos["current_price"].map(lambda v: f"₹{v:,.2f}"),
    "Invested":  sorted_pos["invested"].map(fmt_inr),
    "Value":     sorted_pos["current_value"].map(fmt_inr),
    "P&L":       sorted_pos["pnl"].map(fmt_inr),
    "Return":    sorted_pos["pnl_pct"].map(fmt_pct),
    "Day P&L":   sorted_pos["day_pnl"].map(fmt_inr),
})

def color_pnl(val):
    if isinstance(val, str):
        if val.startswith("-") or val.startswith("−"): return "color: #A84B2F; font-family: monospace;"
        if val.startswith("+") or val.startswith("₹") or val.startswith("-₹"):
            if val.startswith("-") or "-₹" in val: return "color: #A84B2F; font-family: monospace;"
            return "color: #4A6B3A; font-family: monospace;"
    return "font-family: monospace;"

styled = display_df.style.map(color_pnl, subset=["P&L", "Return", "Day P&L"])
st.dataframe(styled, use_container_width=True, hide_index=True, height=min(480, 50 + len(display_df) * 38))

# ─────────────────────────────────────────────────────────────────────────
# CHARTS
# ─────────────────────────────────────────────────────────────────────────

st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)
st.markdown("## Visual *analysis*")

PALETTE = ["#8B3A1F", "#2A5D5E", "#B8893A", "#4A6B3A", "#6E522B", "#944454",
           "#1B474D", "#A0704A", "#3D6B7D", "#7B5E3C", "#6B3D3D", "#5E6B3D"]

c1, c2 = st.columns(2)

with c1:
    fig = px.pie(
        sorted_pos, values="current_value", names="name", hole=0.5,
        color_discrete_sequence=PALETTE,
    )
    fig.update_traces(textposition="inside", textinfo="percent",
                      marker=dict(line=dict(color="#F4F1E8", width=2)))
    fig.update_layout(
        title=dict(text="<b>Allocation by current value</b>", font=dict(family="Fraunces", size=18)),
        showlegend=True, legend=dict(orientation="v", x=1.02, y=0.5, font=dict(size=11)),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=20, t=60, b=20), height=380,
    )
    st.plotly_chart(fig, use_container_width=True)

with c2:
    pnl_sorted = sorted_pos.sort_values("pnl")
    colors = ["#4A6B3A" if v >= 0 else "#A84B2F" for v in pnl_sorted["pnl"]]
    fig = go.Figure(go.Bar(
        y=pnl_sorted["name"], x=pnl_sorted["pnl"], orientation="h",
        marker=dict(color=colors),
        text=pnl_sorted["pnl"].map(fmt_inr), textposition="outside",
        textfont=dict(family="JetBrains Mono", size=10),
    ))
    fig.update_layout(
        title=dict(text="<b>Total P&L per holding</b>", font=dict(family="Fraunces", size=18)),
        xaxis=dict(title="P&L (INR)", gridcolor="#E5DFCC"),
        yaxis=dict(title=""),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=80, t=60, b=40), height=380,
        font=dict(family="Inter", color="#4A463C"),
    )
    st.plotly_chart(fig, use_container_width=True)

c3, c4 = st.columns(2)

with c3:
    # Asset-class split by display_type
    by_class = sorted_pos.groupby("display_type")["current_value"].sum().reset_index()
    fig = px.pie(
        by_class, values="current_value", names="display_type", hole=0.65,
        color_discrete_sequence=PALETTE,
    )
    fig.update_traces(textposition="inside", textinfo="percent+label",
                      marker=dict(line=dict(color="#F4F1E8", width=3)))
    fig.update_layout(
        title=dict(text="<b>By asset class</b>", font=dict(family="Fraunces", size=18)),
        showlegend=False,
        annotations=[dict(text=fmt_inr(total_current), x=0.5, y=0.5, showarrow=False,
                          font=dict(family="Fraunces", size=16))],
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=20, t=60, b=20), height=380,
    )
    st.plotly_chart(fig, use_container_width=True)

with c4:
    by_value = sorted_pos.sort_values("current_value", ascending=True)
    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=by_value["name"], x=by_value["invested"], orientation="h",
        name="Invested", marker=dict(color="#8A8576"),
    ))
    fig.add_trace(go.Bar(
        y=by_value["name"], x=by_value["current_value"], orientation="h",
        name="Current", marker=dict(color="#8B3A1F"),
    ))
    fig.update_layout(
        title=dict(text="<b>Invested vs current value</b>", font=dict(family="Fraunces", size=18)),
        xaxis=dict(title="INR", gridcolor="#E5DFCC"),
        barmode="group", legend=dict(orientation="h", y=-0.1),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=40, t=60, b=60), height=380,
        font=dict(family="Inter", color="#4A463C"),
    )
    st.plotly_chart(fig, use_container_width=True)

# Footer
st.markdown("---")

# ─── Historical timeline chart ───────────────────────────────────────────
st.markdown("## Portfolio *over time*")
st.caption("Total value vs invested cost vs cumulative P&L, day by day from your first transaction.")

range_col, bd_col = st.columns([3, 1])
with range_col:
    range_pick = st.radio(
        "Range", ["1M", "3M", "6M", "1Y", "All"],
        horizontal=True, index=3, label_visibility="collapsed",
    )
with bd_col:
    show_breakdown = st.toggle("Per-holding breakdown", value=False)

with st.spinner("Computing historical timeline… (first run may take a moment)"):
    try:
        timeline = build_historical_timeline(
            txs_df.to_json(date_format="iso"),
            positions.to_json(),
        )
    except Exception as e:
        st.warning(f"Could not build timeline: {e}")
        timeline = pd.DataFrame()

if timeline.empty:
    st.info("Not enough data to build a timeline yet. Add more transactions and try again.")
else:
    # Aggregate per-day totals
    totals = (
        timeline.groupby("date")
        .agg(value=("value", "sum"), invested=("invested", "sum"))
        .reset_index()
    )
    totals["pnl"] = totals["value"] - totals["invested"]

    # Apply range filter
    today_ts = pd.Timestamp(date.today())
    days_map  = {"1M": 30, "3M": 90, "6M": 180, "1Y": 365, "All": 99999}
    cutoff    = today_ts - pd.Timedelta(days=days_map[range_pick])
    totals    = totals[totals["date"] >= cutoff]
    tl_slice  = timeline[timeline["date"] >= cutoff]

    PALETTE = ["#8B3A1F","#2A5D5E","#B8893A","#4A6B3A","#6E522B","#944454",
               "#1B474D","#A0704A","#3D6B7D","#7B5E3C"]

    if show_breakdown:
        # Stacked area — one band per holding
        fig = px.area(
            tl_slice, x="date", y="value", color="name",
            color_discrete_sequence=PALETTE,
            labels={"value": "Value (INR)", "date": "", "name": "Holding"},
        )
        fig.update_traces(mode="lines")
        fig.update_layout(
            title=dict(text="<b>Portfolio value by holding, stacked</b>",
                       font=dict(family="Fraunces", size=20)),
            xaxis=dict(gridcolor="#E5DFCC"),
            yaxis=dict(title="Value (INR)", gridcolor="#E5DFCC"),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=20, r=20, t=60, b=20), height=460,
            hovermode="x unified",
            legend=dict(orientation="v", x=1.01, y=0.5, font=dict(size=11)),
        )
    else:
        # Three-line chart: value + invested (left axis) + P&L (right axis)
        fig = go.Figure()

        # Shaded area between invested and value (green = gain, red = loss)
        # Split into gain and loss segments for correct coloring
        for _, row in totals.iterrows():
            pass  # we'll use a simpler approach: fill between via traces

        fig.add_trace(go.Scatter(
            x=totals["date"], y=totals["value"],
            name="Current Value",
            line=dict(color="#8B3A1F", width=2.5),
            fill=None,
        ))
        fig.add_trace(go.Scatter(
            x=totals["date"], y=totals["invested"],
            name="Invested Cost",
            line=dict(color="#8A8576", width=1.8, dash="dash"),
            fill="tonexty",
            fillcolor="rgba(74,107,58,0.08)",  # faint green between the lines
        ))
        fig.add_trace(go.Scatter(
            x=totals["date"], y=totals["pnl"],
            name="Cumulative P&L",
            line=dict(color="#2A5D5E", width=2),
            yaxis="y2",
        ))

        # Zero line for P&L axis
        fig.add_hline(y=0, line=dict(color="#D9D3C0", width=1, dash="dot"),
                      annotation=None)

        fig.update_layout(
            title=dict(text="<b>Value, invested cost &amp; cumulative P&L</b>",
                       font=dict(family="Fraunces", size=20)),
            xaxis=dict(title="", gridcolor="#E5DFCC"),
            yaxis=dict(title="INR", gridcolor="#E5DFCC"),
            yaxis2=dict(
                title="P&L (INR)",
                overlaying="y", side="right",
                gridcolor="rgba(0,0,0,0)",
                zeroline=True, zerolinecolor="#D9D3C0",
            ),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(l=20, r=60, t=60, b=40), height=460,
            hovermode="x unified",
            legend=dict(orientation="h", y=-0.12, font=dict(size=12)),
        )

    st.plotly_chart(fig, use_container_width=True)

    # Quick stats below the chart
    if not totals.empty:
        peak_val = totals["value"].max()
        peak_date = totals.loc[totals["value"].idxmax(), "date"].strftime("%d %b %Y")
        latest_pnl = totals["pnl"].iloc[-1]
        latest_pct = (latest_pnl / totals["invested"].iloc[-1] * 100) if totals["invested"].iloc[-1] > 0 else 0
        s1, s2, s3 = st.columns(3)
        s1.metric("Peak Value (period)", fmt_inr(peak_val), delta=peak_date)
        s2.metric("P&L Today", fmt_inr(latest_pnl), delta=fmt_pct(latest_pct))
        days_in_market = (date.today() - txs_df["date"].min().date()).days
        s3.metric("Days invested", f"{days_in_market:,}")

st.markdown("---")
st.caption(
    f"Generated {datetime.now().strftime('%d %b %Y %I:%M %p')} · "
    f"{len(txs_df)} transactions → {len(positions)} positions"
)
