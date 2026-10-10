# StockSignal

**AI stock signals, right in your terminal.**

Analyze up to 5 stocks with **yfinance**, the **Olostep Python SDK** and the **TypeSafe Python SDK (Jev)**. Get color-coded BUY / HOLD / SELL decisions, probability breakdowns, market impact and news links for a **5-trading-day horizon**.

![StockSignal terminal dashboard — fictional demo values](assets/stocksignal.png)

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11–3.13 (Olostep SDK compatibility).

```bash
uv sync
```

Copy `.env.example` to `.env` and add your keys:

```env
OLOSTEP_API_KEY=your_key_here
JEV_API_KEY=your_key_here
```

```bash
uv run app
```

Enter 1–5 tickers, then press **R**. **A** add · **D** delete · **↑/↓** select · **Esc** focus stocks · **R** refresh · **Q** quit.

Try the dashboard without keys: `uv run app --demo`.

## How it works

```mermaid
flowchart LR
    T[Tickers] --> M[yfinance · market data]
    T --> N[Olostep · recent news]
    M --> J[TypeSafe Jev · choice + score]
    N --> J
    J --> D[Terminal dashboard]
```

**Market impact (1–5)** rates the significance of recent developments, from Very Low to Critical. **Confidence** is the selected signal's model-estimated preference. These preferences are **not calibrated probabilities of future returns**.
