"""StockSignal: a compact, asynchronous stock analysis terminal dashboard."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import os
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from dotenv import load_dotenv
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Input, Link, LoadingIndicator, Static

from . import jev, market, olostep

COLORS = {"BUY": "#4ade80", "HOLD": "#facc15", "SELL": "#f87171"}
DEMO = {
    "NVDA": (185.40, 2.10, 4.2e12, 195.00, 86.20, (72, 20, 8), 4, 3.8, 9.2),
    "AAPL": (250.10, -0.50, 3.7e12, 275.00, 169.21, (30, 50, 20), 2, -1.1, 2.5),
    "MSFT": (520.30, -1.20, 3.9e12, 555.00, 344.79, (15, 25, 60), 4, -3.2, -4.7),
    "TSLA": (310.20, 0.80, 1.0e12, 488.54, 138.80, (25, 55, 20), 3, 1.4, -2.9),
    "AMZN": (225.70, 1.30, 2.4e12, 242.52, 151.61, (65, 25, 10), 3, 2.8, 5.4),
}


def demo_result(ticker: str) -> dict:
    if ticker not in DEMO:
        raise ValueError("Demo supports NVDA, AAPL, MSFT, TSLA and AMZN only. Use live mode for other stocks.")
    price, change, cap, high, low, probs, impact, week, month = DEMO[ticker]
    probabilities = dict(zip(jev.SIGNALS, probs))
    signal = max(probabilities, key=probabilities.get)
    return {
        "market": {"ticker": ticker, "company": f"{ticker} — fictional demo values", "currency": "USD",
                   "price": price, "change_percent": change, "market_cap": cap, "high_52w": high,
                   "low_52w": low, "return_5_sessions_percent": week, "return_20_sessions_percent": month,
                   "price_basis": "Fictional demo price", "price_as_of": "DEMO", "history_latest_session": "DEMO"},
        "news": {"articles": [], "status": "DEMO — no live news retrieved.", "date_status": "Sample mode: API calls disabled.",
                 "attempts": 0},
        "decision": {"signal": signal, "probabilities": probabilities, "confidence": probabilities[signal],
                     "impact": impact, "impact_score": float(impact), "model": "DEMO / sample values",
                     "usage": {"input_tokens": 0, "output_tokens": 0},
                     "elapsed_seconds": None, "attempts": 0},
        "status": "DEMO", "error": None, "analyzed_at": "DEMO", "elapsed_seconds": None,
    }


def money(value, currency="USD", *, compact=False) -> str:
    if value is None:
        return "—"
    prefix = "$" if currency == "USD" else f"{currency or '?'} "
    if compact:
        for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
            if value >= threshold:
                return f"{prefix}{value / threshold:.2f}{suffix}"
    return f"{prefix}{value:,.2f}"


def percent(value) -> str:
    return "—" if value is None else f"{value:+.2f}%"


class StockSignal(App):
    TITLE = "StockSignal"
    CSS = """
    Screen { background: #0b1019; color: #e6edf7; }
    #brand { height: 4; border: round #334760; padding: 0 2; margin: 1 2 0 2; }
    #mode { height: auto; margin: 0 3; color: #8ba1ba; }
    #controls { height: 3; margin: 1 2 0 2; }
    #ticker-input { width: 1fr; margin-right: 1; }
    Button { min-width: 10; margin-right: 1; }
    #watchlist, #status, #run-summary { height: auto; margin: 0 3; }
    #watchlist { color: #8ba1ba; }
    #status { color: #b0c6df; }
    #run-summary { color: #8ba1ba; }
    #loading { height: 1; display: none; }
    #stocks { height: 3; margin: 1 2 0 2; border: round #334760; }
    DataTable > .datatable--cursor { background: #1c3048; }
    #details { height: 1fr; min-height: 5; margin: 1 2 0 2; border: round #334760; padding: 0 1; }
    #metrics { height: auto; }
    #sources { height: auto; margin-top: 1; }
    Link { height: auto; margin-bottom: 1; color: #77b7ff; }
    #notice { height: auto; margin: 0 3; color: #8ba1ba; }
    Footer { background: #172235; }
    """
    BINDINGS = [
        ("a", "add", "Add"), ("d", "delete", "Delete"), ("r", "refresh", "Run / Refresh"),
        ("escape", "table", "Stocks"), ("q", "quit", "Quit"), ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, tickers: str = "", *, demo=False):
        super().__init__()
        self.demo = demo
        self.initial_tickers = tickers or ("NVDA, AAPL, MSFT" if demo else "")
        self.results: dict[str, dict] = {}
        self.selected: str | None = None
        self.busy = False
        self.details_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble(("STOCKSIGNAL\n", "bold cyan"), "AI Stock Intelligence  |  Olostep + TypeSafe Jev"), id="brand")
        yield Static("DEMO — fictional sample values; no API requests" if self.demo else "LIVE  •  Real market data  •  Horizon: 5 trading days", id="mode")
        with Horizontal(id="controls"):
            yield Input(placeholder="1–5 stock tickers: NVDA, AAPL, MSFT", id="ticker-input")
            yield Button("Add", id="add", variant="primary")
            yield Button("Run", id="run", variant="success")
            yield Button("Delete", id="delete")
        yield Static("Tickers: none  (0/5)", id="watchlist", markup=False)
        yield Static("Add stock tickers to begin.", id="status", markup=False)
        yield Static("Last run: —", id="run-summary", markup=False)
        yield LoadingIndicator(id="loading")
        yield DataTable(id="stocks", cursor_type="row", zebra_stripes=True, cursor_foreground_priority="renderable")
        with VerticalScroll(id="details"):
            yield Static("Select a stock to inspect its metrics.", id="metrics", markup=False)
            with VerticalScroll(id="sources"):
                pass
        yield Static("Model-estimated decision preferences; not calibrated probabilities of future stock returns.", id="notice", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#stocks", DataTable).add_columns("TICKER", "PRICE", "CHANGE", "SIGNAL", "BUY%", "HOLD%", "SELL%", "IMPACT", "STATUS")
        self.query_one("#ticker-input", Input).focus()
        if self.initial_tickers:
            self.query_one("#ticker-input", Input).value = self.initial_tickers
            self.add_tickers()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.query_one("#loading").display = busy
        for widget in self.query("#controls Button, #ticker-input"):
            widget.disabled = busy

    def set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def action_add(self) -> None:
        if not self.busy:
            self.query_one("#ticker-input", Input).focus()

    def action_table(self) -> None:
        self.query_one("#stocks", DataTable).focus()

    @on(Input.Submitted, "#ticker-input")
    @on(Button.Pressed, "#add")
    def submit_tickers(self) -> None:
        if not self.busy:
            self.add_tickers()

    @work(group="operations", exclusive=True)
    async def add_tickers(self) -> None:
        if self.busy:
            return
        try:
            tickers = market.parse_tickers(self.query_one("#ticker-input", Input).value, list(self.results))
            if self.demo:
                additions = [demo_result(t) for t in tickers]
            else:
                self.set_busy(True)
                self.set_status("Validating stock symbols with Yahoo Finance…")
                snapshots = await asyncio.gather(*(market.fetch_market(t) for t in tickers), return_exceptions=True)
                failures = [s for s in snapshots if isinstance(s, Exception)]
                if failures:
                    raise ValueError(" ".join(str(e) for e in failures))
                additions = [{"market": s, "decision": None, "news": None, "status": "Ready",
                              "error": None, "analyzed_at": None, "elapsed_seconds": None} for s in snapshots]
            for ticker, result in zip(tickers, additions):
                self.results[ticker] = result
            self.selected = self.selected or tickers[0]
            self.query_one("#ticker-input", Input).value = ""
            self.set_status("Sample values loaded." if self.demo else "Tickers validated. Press R to run analysis.")
            self.render_table()
            await self.render_details()
        except ValueError as error:
            self.set_status(str(error))
        finally:
            self.set_busy(False)
            self.action_table()

    @on(Button.Pressed, "#delete")
    def action_delete(self) -> None:
        if self.busy or not self.selected:
            return
        self.results.pop(self.selected, None)
        self.selected = next(iter(self.results), None)
        self.render_table()
        self.run_worker(self.render_details())
        self.set_status("Stock removed from the watchlist.")

    @on(Button.Pressed, "#run")
    def action_refresh(self) -> None:
        if self.busy:
            return
        if not self.results:
            self.set_status("Add at least one stock before running analysis.")
            self.action_add()
            return
        if not self.demo:
            missing = [name for name in ("OLOSTEP_API_KEY", "JEV_API_KEY") if os.getenv(name, "").strip() in ("", "your_key_here")]
            if missing:
                self.set_status(f"Set {', '.join(missing)} in .env, then restart StockSignal.")
                return
        self.refresh_analysis()

    @work(group="operations", exclusive=True)
    async def refresh_analysis(self) -> None:
        started = perf_counter()
        self.set_busy(True)
        self.set_status("Refreshing samples…" if self.demo else "Analyzing stocks…")
        self.query_one("#run-summary", Static).update("Run time: running…")
        for result in self.results.values():
            result.update(decision=None, news=None, error=None, analyzed_at=None, elapsed_seconds=None, status="Loading")
        self.render_table()
        await self.render_details()
        try:
            async with AsyncExitStack() as stack:
                news_client = decision_client = None
                if not self.demo:
                    news_client = await stack.enter_async_context(olostep.AsyncOlostep(
                        api_key=os.environ["OLOSTEP_API_KEY"].strip(),
                        retry_strategy=olostep.RetryStrategy(max_retries=3),
                    ))
                    decision_client = await stack.enter_async_context(jev.AsyncTypeSafeClient(
                        api_key=os.environ["JEV_API_KEY"].strip(), model="jev-latest", timeout=45,
                    ))
                async def run_one(ticker):
                    result = self.results[ticker]
                    stock_started = perf_counter()
                    try:
                        if self.demo:
                            self.results[ticker] = demo_result(ticker)
                        else:
                            result["status"] = "Market + News"
                            self.render_table()
                            # News needs the validated identity, so it can overlap the price refresh.
                            identity = {"ticker": ticker, "company": result["market"]["company"]}
                            data, news = await asyncio.gather(
                                market.fetch_market(ticker), olostep.fetch_news(news_client, identity),
                                return_exceptions=True,
                            )
                            if not isinstance(data, Exception):
                                result["market"] = data
                            if not isinstance(news, Exception):
                                result["news"] = news
                            for response in (data, news):
                                if isinstance(response, Exception):
                                    raise response
                            result["status"] = "Jev"
                            self.render_table()
                            result["decision"] = await jev.analyze(decision_client, result["market"], result["news"])
                            result["analyzed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                            result["status"] = "Done"
                    except ValueError as error:
                        result.update(status="Error", error=str(error))
                    except Exception:
                        result.update(status="Error", error="Unexpected analysis failure. Retry the analysis.")
                    finally:
                        self.results[ticker]["elapsed_seconds"] = perf_counter() - stock_started
                    self.render_table()
                    if self.selected == ticker:
                        await self.render_details()
                await asyncio.gather(*(run_one(t) for t in self.results))
            self.render_run_summary(perf_counter() - started)
            failed = sum(r["status"] == "Error" for r in self.results.values())
            self.set_status(f"Analysis complete: {len(self.results) - failed}/{len(self.results)} stocks ready" + (f"; {failed} failed. Select a stock for its error." if failed else "."))
        except (jev.TypeSafeError, olostep.errors.Olostep_BaseError, ValueError):
            for result in self.results.values():
                result.update(status="Error", error="Could not initialize API clients. Check your API keys.")
            self.render_table()
            await self.render_details()
            self.render_run_summary(perf_counter() - started)
            self.set_status("Could not initialize API clients. Check your API keys.")
        finally:
            self.set_busy(False)

    def render_run_summary(self, elapsed: float) -> None:
        self.query_one("#run-summary", Static).update(
            f"Last run: {elapsed:.2f}s" + ("  |  DEMO: no API calls" if self.demo else "")
        )

    def render_table(self) -> None:
        table = self.query_one("#stocks", DataTable)
        rebuild = [key.value for key in table.rows] != list(self.results)
        if rebuild:
            table.clear()
        table.styles.height = max(3, len(self.results) + 3)
        for ticker, result in self.results.items():
            data, decision = result["market"], result["decision"]
            change = data["change_percent"]
            signal = decision["signal"] if decision else "—"
            probabilities = decision["probabilities"] if decision else {}
            cells = (ticker, money(data["price"], data["currency"]),
                     Text(percent(change), style="dim" if change is None else COLORS["BUY" if change >= 0 else "SELL"]),
                     Text(signal, style=f"bold {COLORS.get(signal, 'dim')}"),
                     *(f"{probabilities[s]:.1f}" if s in probabilities else "—" for s in jev.SIGNALS),
                     f"{decision['impact']}/5" if decision else "—", result["status"])
            if rebuild:
                table.add_row(*cells, key=ticker)
            else:
                for column, cell in zip(table.columns, cells):
                    table.update_cell(ticker, column, cell, update_width=True)
        if rebuild and self.selected in self.results:
            table.move_cursor(row=list(self.results).index(self.selected))
        self.query_one("#watchlist", Static).update(f"Tickers: {', '.join(self.results) or 'none'}  ({len(self.results)}/5)")

    @on(DataTable.RowHighlighted, "#stocks")
    @on(DataTable.RowSelected, "#stocks")
    async def select_stock(self, event) -> None:
        ticker = str(event.row_key.value)
        if ticker in self.results:
            self.selected = ticker
            await self.render_details()
            if isinstance(event, DataTable.RowSelected):
                self.query_one("#details").focus()

    async def render_details(self) -> None:
        async with self.details_lock:
            if self.query("#sources"):
                await self._render_details()

    async def _render_details(self) -> None:
        sources = self.query_one("#sources", VerticalScroll)
        await sources.remove_children()
        if self.selected not in self.results:
            self.query_one("#metrics", Static).update("Select a stock to inspect its metrics.")
            return
        result = self.results[self.selected]
        data, decision, news = result["market"], result["decision"], result["news"]
        text = Text.assemble((f"{self.selected}  ", "bold cyan"), (f"{data['company']}\n", "dim"))
        if result["error"]:
            text.append(f"{result['error']}\n", style="bold red")
        if decision:
            text.append(f"{decision['signal']}  ", style=f"bold {COLORS[decision['signal']]}")
            text.append(f"Confidence: {decision['confidence']:.1f}%  |  Market Impact: {decision['impact']}/5 ({jev.IMPACT_LEVELS[decision['impact'] - 1]})\n")
            text.append(f"Horizon: 5 trading days  |  Impact mean: {decision['impact_score']:.2f}/5\n", style="dim")
            for signal in jev.SIGNALS:
                probability = decision["probabilities"][signal]
                filled = round(probability / 100 * 24)
                text.append(f"{signal:<5} ", style=COLORS[signal])
                text.append("━" * filled, style=COLORS[signal])
                text.append("░" * (24 - filled), style="dim")
                text.append(f" {probability:5.1f}%\n")
            text.append(f"Model: {decision['model']}  |  Analyzed: {result['analyzed_at']}\n", style="dim")
        else:
            text.append(f"Signal unavailable  |  {result['status']}  |  Horizon: 5 trading days\n", style="yellow")
        elapsed = result.get("elapsed_seconds")
        if elapsed is not None:
            text.append(f"Full analysis: {elapsed:.2f}s (market + news + Jev)\n", style="dim")
        if decision:
            duration = decision.get("elapsed_seconds")
            duration_text = f"{duration:.3f}s" if duration is not None else "—"
            text.append(f"Jev request: {duration_text} (incl. network/retries)\n", style="dim")
            usage = decision.get("usage") or {}
            text.append(f"Jev tokens: {usage.get('input_tokens') if usage.get('input_tokens') is not None else '—'} input / "
                        f"{usage.get('output_tokens') if usage.get('output_tokens') is not None else '—'} output\n", style="dim")
        if self.demo:
            text.append("DEMO: no API requests.\n", style="dim")
        metrics = Table.grid(padding=(0, 2))
        metrics.add_column(style="dim")
        metrics.add_column()
        for label, value in (
            ("Price", money(data["price"], data["currency"])), ("Daily change", percent(data["change_percent"])),
            ("Market cap", money(data["market_cap"], data["currency"], compact=True)),
            ("52-week high / low", f"{money(data['high_52w'], data['currency'])} / {money(data['low_52w'], data['currency'])}"),
            ("5 / 20-session return", f"{percent(data['return_5_sessions_percent'])} / {percent(data['return_20_sessions_percent'])}"),
            ("Price basis / as of", f"{data['price_basis']} / {data['price_as_of']}"),
            ("Historical returns", f"Adjusted closes through {data['history_latest_session']}"),
        ):
            metrics.add_row(label, value)
        self.query_one("#metrics", Static).update(Group(text, metrics))
        await sources.mount(Static(news["status"] if news else "News not retrieved yet.", markup=False))
        if news:
            await sources.mount(Static(news["date_status"], markup=False))
            for article in news["articles"]:
                await sources.mount(Link(f"{article['title']}\n{article['url']}\n{article['content_basis']}", url=article["url"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="StockSignal — structured AI stock signals for a 5-trading-day horizon")
    parser.add_argument("tickers", nargs="*", help="Optional initial stocks (space or comma separated)")
    parser.add_argument("--demo", action="store_true", help="Use labeled fictional samples without API keys or network calls")
    args = parser.parse_args()
    load_dotenv(Path.cwd() / ".env")
    load_dotenv()
    StockSignal(" ".join(args.tickers), demo=args.demo).run()


if __name__ == "__main__":
    main()
