"""Contract and headless UI checks; no API credits required."""

import ast
import asyncio
import copy
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import httpx2
import pandas as pd
from textual.widgets import DataTable, Input, Link, Static
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy
from olostep import AsyncOlostep

from stocksignal import jev, market, olostep
from stocksignal.app import StockSignal, demo_result
from examples import simple
from examples.simple import analyze_tickers


def decision_payload():
    return {"model": "jev-1.13.0", "usage": {"input_tokens": 12500, "output_tokens": 85}, "answers": {
        "signal": {"type": "choice", "choice": "BUY", "probabilities": {"BUY": .72, "HOLD": .20, "SELL": .08}, "confidence": .5},
        "impact": {"type": "score", "score": 2.7, "confidence": .7,
                   "legend": {str(i): str(i) for i in range(5)},
                   "probabilities": {"0": 0., "1": 0., "2": .3, "3": .7, "4": 0.}},
    }}


class DataTests(unittest.TestCase):
    def test_tickers(self):
        self.assertEqual(market.parse_tickers("nvda, AAPL brk-b, "), ["NVDA", "AAPL", "BRK-B"])
        for text, existing in (("", []), ("NVDA nvda", []), ("aapl", ["AAPL"]),
                               ("A B C D E F", []), ("$AAPL", []), ("AAPL/", []), (",,,", [])):
            with self.subTest(text=text), self.assertRaises(ValueError):
                market.parse_tickers(text, existing)

    def test_market_and_fallback(self):
        history = pd.DataFrame({"Close": list(range(100, 122)), "Adj Close": list(range(100, 122))},
                               index=pd.date_range("2026-09-01", periods=22, freq="B"))
        with patch.object(market.yf, "Ticker") as ticker:
            ticker.return_value.history.return_value = history
            ticker.return_value.get_info.return_value = {"quoteType": "EQUITY", "currency": "USD"}
            result = market._fetch_market("AAPL")
            self.assertEqual(result["price"], 121)
            self.assertAlmostEqual(result["change_percent"], (121 / 120 - 1) * 100)
            self.assertIsNone(result["market_cap"])
            self.assertAlmostEqual(result["return_5_sessions_percent"], (121 / 116 - 1) * 100)
            self.assertEqual(len(result["recent_history"]), 21)
            json.dumps(result, allow_nan=False)
            ticker.return_value.get_info.return_value.update(regularMarketPrice=123, regularMarketTime=1_780_000_000, regularMarketPreviousClose=121)
            quote = market._fetch_market("AAPL")
            self.assertEqual(quote["price"], 123)
            self.assertAlmostEqual(quote["change_percent"], (123 / 121 - 1) * 100)
            ticker.return_value.get_info.return_value["quoteType"] = "ETF"
            with self.assertRaises(ValueError):
                market._fetch_market("SPY")

    def test_finite_values(self):
        for value in (None, True, float("nan"), float("inf"), "unknown"):
            self.assertIsNone(market.number(value))

    def test_decision_contract(self):
        decision = jev.parse_decision(decision_payload())
        self.assertEqual(decision["signal"], "BUY")
        self.assertEqual(decision["confidence"], 72)
        self.assertEqual(sum(decision["probabilities"].values()), 100)
        self.assertEqual(decision["impact"], 4)
        self.assertAlmostEqual(decision["impact_score"], 3.7)
        self.assertEqual(decision["usage"], {"input_tokens": 12500, "output_tokens": 85})
        for score, expected in ((0, 1), (4, 5), (1.5, 3)):
            payload = decision_payload()
            payload["answers"]["impact"]["score"] = score
            self.assertEqual(jev.parse_decision(payload)["impact"], expected)

    def test_missing_or_invalid_usage_does_not_invent_tokens(self):
        for usage in (None, {}, "unknown", {"input_tokens": True}, {"input_tokens": -1},
                      {"input_tokens": 100.5}, {"input_tokens": "12500"}):
            payload = decision_payload()
            payload["usage"] = usage
            with self.subTest(usage=usage):
                decision = jev.parse_decision(payload)
                self.assertEqual(decision["signal"], "BUY")
                self.assertIsNone(decision["usage"]["input_tokens"])

    def test_reject_invalid_decisions(self):
        for probabilities in ({"BUY": .8, "HOLD": .2}, {"BUY": .2, "HOLD": .2, "SELL": .2},
                              {"BUY": float("nan"), "HOLD": .2, "SELL": .08},
                              {"BUY": True, "HOLD": 0, "SELL": 0}, {"BUY": 1.1, "HOLD": -.1, "SELL": 0}):
            payload = decision_payload()
            payload["answers"]["signal"]["probabilities"] = probabilities
            with self.subTest(probabilities=probabilities), self.assertRaises(ValueError):
                jev.parse_decision(payload)
        for path, value in (("choice", "SELL"), ("choice", "WAIT")):
            payload = decision_payload()
            payload["answers"]["signal"][path] = value
            with self.assertRaises(ValueError):
                jev.parse_decision(payload)
        payload = decision_payload()
        payload["answers"]["impact"]["score"] = 5
        with self.assertRaises(ValueError):
            jev.parse_decision(payload)

    def test_news_deduplication_and_empty(self):
        articles = olostep.news_from_response({"result": {"links": [
            {"url": "https://news.example/story?utm_source=x", "title": "Company announces earnings", "markdown_content": "Extracted announcement"},
            {"url": "https://news.example/story#top", "title": "Other title", "description": "Duplicate URL"},
            {"url": "https://other.example/story", "title": "Company announces earnings!", "description": "Syndicated"},
            {"url": "javascript:alert(1)", "title": "Bad link", "description": "Skip"},
            {"url": "https://finance.yahoo.com/quote/NVDA", "title": "Stock quote", "description": "Skip"},
            {"url": "https://news.example/second", "title": "Regulatory filing", "description": "Snippet"},
        ]}})
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["url"], "https://news.example/story")
        self.assertEqual(articles[0]["excerpt"], "Extracted announcement")
        self.assertEqual(articles[1]["content_basis"], "Search snippet only")
        self.assertEqual(olostep.news_from_response({"result": {"links": []}}), [])
        with self.assertRaises(ValueError):
            olostep.news_from_response({})

    def test_social_sources_filtered_locally(self):
        urls = ["https://facebook.com/post", "https://www.reddit.com/story", "https://x.com/company/status/1",
                "https://twitter.com/post", "https://instagram.com/post", "https://x.com.example.org/announcement",
                "https://investor.nvidia.com/news/announcement"]
        articles = olostep.news_from_response({"result": {"links": [
            {"url": url, "title": f"Article {i} {url}", "description": "Company announcement"}
            for i, url in enumerate(urls)
        ]}})
        self.assertEqual([article["url"] for article in articles], urls[-2:])

    def test_quote_and_calendar_pages_are_not_news(self):
        for url in ("https://www.cnbc.com/quotes/MU", "https://seekingalpha.com/symbol/MU",
                    "https://www.coinbase.com/stock/SPXC", "https://www.alphaquery.com/stock/MU/earnings-history",
                    "https://www.zacks.com/stock/research/MU/earnings-calendar"):
            with self.subTest(url=url):
                self.assertEqual(olostep.news_from_response({"result": {"links": [
                    {"url": url, "description": "Market data page"}
                ]}}), [])


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_contracts(self):
        requests = []
        async def respond_olo(client, request, **kwargs):
            requests.append(request)
            return httpx.Response(200, request=request, json={
                "id": "search-test", "created": 1, "query": "news",
                "result": {"links": []},
            })
        def respond_jev(request):
            requests.append(request)
            return httpx2.Response(200, json=decision_payload())
        with patch.object(httpx.AsyncClient, "send", new=respond_olo):
            async with AsyncOlostep(api_key="test-olo") as client:
                news = await olostep.fetch_news(client, demo_result("NVDA")["market"])
                self.assertIn("No relevant news", news["status"])
        async with AsyncTypeSafeClient(api_key="test-jev", transport=httpx2.MockTransport(respond_jev)) as client:
            with patch.object(jev, "perf_counter", side_effect=[10, 10.25]):
                decision = await jev.analyze(client, {}, news)
            self.assertEqual(decision["confidence"], 72)
            self.assertEqual(decision["elapsed_seconds"], .25)
        self.assertEqual(str(requests[0].url), "https://api.olostep.com/v1/searches")
        self.assertEqual(str(requests[1].url), "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-olo")
        self.assertEqual(requests[1].headers["Authorization"], "Bearer test-jev")
        body = json.loads(requests[1].content)
        self.assertEqual(body["state"]["prediction_horizon_trading_days"], 5)
        self.assertEqual(body["questions"]["signal"]["type"], "choice")
        self.assertEqual(body["questions"]["impact"]["type"], "score")
        self.assertEqual(len(body["questions"]["impact"]["criteria"]), 5)
        self.assertEqual(body["model"], "jev-latest")
        search = json.loads(requests[0].content)
        self.assertEqual(search["scrape_options"]["formats"], ["markdown"])
        self.assertNotIn("exclude_domains", search)
        self.assertNotIn("fast_mode", search)  # Not supported by the SDK.
        self.assertIn("after:", search["query"])

    async def test_retry_and_http_error(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx2.Response(429 if len(calls) == 1 else 200, json=decision_payload())
        async with AsyncTypeSafeClient(api_key="test", transport=httpx2.MockTransport(respond),
                                       retry=RetryPolicy(backoff_initial=0, backoff_jitter=0)) as client:
            with patch.object(jev, "perf_counter", side_effect=[10, 11.25]):
                decision = await jev.analyze(client, {}, {})
            self.assertEqual(decision["elapsed_seconds"], 1.25)
        self.assertEqual(len(calls), 2)
        async with AsyncTypeSafeClient(api_key="test", transport=httpx2.MockTransport(
                lambda r: httpx2.Response(401, json={"message": "secret-server-detail"}))) as client:
            with self.assertRaisesRegex(ValueError, "Jev HTTP 401") as raised:
                await jev.analyze(client, {}, {})
            self.assertNotIn("secret-server-detail", str(raised.exception))

    async def test_olostep_retry_and_safe_errors(self):
        calls = []
        async def retry(client, request, **kwargs):
            calls.append(request)
            return httpx.Response(501 if len(calls) == 1 else 200, request=request, json={"max_capacity_reached": True} if len(calls) == 1 else {
                "id": "search-test", "created": 1, "query": "news", "result": {"links": []},
            })
        with patch.object(httpx.AsyncClient, "send", new=retry):
            async with AsyncOlostep(api_key="test", retry_strategy=olostep.RetryStrategy(
                    max_retries=2, initial_delay=0, jitter_min=0, jitter_max=0)) as client:
                await olostep.fetch_news(client, demo_result("NVDA")["market"])
        self.assertEqual(len(calls), 2)
        for status, body, message in (
            (401, {"invalid_api_key": True}, "authentication failed"),
            (402, {"usage_limit_reached": True, "message": "secret-server-detail"}, "credits exhausted"),
        ):
            async def fail(client, request, **kwargs):
                return httpx.Response(status, request=request, json=body)
            with patch.object(httpx.AsyncClient, "send", new=fail):
                async with AsyncOlostep(api_key="secret-test-key") as client:
                    with self.assertRaisesRegex(ValueError, message) as raised:
                        await olostep.fetch_news(client, demo_result("NVDA")["market"])
            self.assertNotIn("secret-test-key", str(raised.exception))
            self.assertNotIn("secret-server-detail", str(raised.exception))

    async def test_missing_key_no_clients(self):
        with patch.dict(os.environ, {"JEV_API_KEY": "", "OLOSTEP_API_KEY": ""}), \
             patch.object(jev, "AsyncTypeSafeClient", side_effect=AssertionError("SDK without key")), \
             patch.object(olostep, "AsyncOlostep", side_effect=AssertionError("SDK without key")):
            app = StockSignal()
            async with app.run_test(size=(100, 32)):
                app.results = {"NVDA": demo_result("NVDA")}
                app.action_refresh()
                self.assertIn("JEV_API_KEY", str(app.query_one("#status", Static).render()))
                self.assertFalse(app.busy)

    async def test_malformed_responses_and_timeout(self):
        for payload in (None, {"model": "test", "usage": {}, "answers": {}}):
            async with AsyncTypeSafeClient(api_key="test", retry=RetryPolicy(max_retries=0),
                    transport=httpx2.MockTransport(lambda r: httpx2.Response(200, text="not JSON")
                        if payload is None else httpx2.Response(200, json=payload))) as client:
                with self.assertRaisesRegex(ValueError, "invalid"):
                    await jev.analyze(client, {}, {})
        async def bad_news(client, request, **kwargs):
            return httpx.Response(200, request=request, text="not JSON")
        with patch.object(httpx.AsyncClient, "send", new=bad_news):
            async with AsyncOlostep(api_key="test") as client:
                with self.assertRaisesRegex(ValueError, "invalid news"):
                    await olostep.fetch_news(client, demo_result("NVDA")["market"])
        def timeout(request):
            raise httpx2.ReadTimeout("test", request=request)
        async with AsyncTypeSafeClient(api_key="test", retry=RetryPolicy(max_retries=0),
                transport=httpx2.MockTransport(timeout)) as client:
            with self.assertRaisesRegex(ValueError, "timed out"):
                await jev.analyze(client, {}, {})
        client = AsyncMock()
        client.searches.create.side_effect = TimeoutError
        with self.assertRaisesRegex(ValueError, "timed out"):
            await olostep.fetch_news(client, demo_result("NVDA")["market"])

    async def test_invalid_key_restores_ui_after_client_setup_failure(self):
        with patch.dict(os.environ, {"JEV_API_KEY": "invalid key", "OLOSTEP_API_KEY": "test"}):
            app = StockSignal()
            async with app.run_test(size=(100, 32)):
                app.results = {"NVDA": demo_result("NVDA")}
                app.selected = "NVDA"
                app.action_refresh()
                await app.workers.wait_for_complete()
                self.assertFalse(app.busy)
                self.assertEqual(app.results["NVDA"]["status"], "Error")
                self.assertIsNone(app.results["NVDA"]["decision"])
                self.assertIn("Check your API keys", app.results["NVDA"]["error"])

    async def test_demo_without_sdk_clients(self):
        with patch.object(jev, "AsyncTypeSafeClient", side_effect=AssertionError("Demo created SDK")), \
             patch.object(olostep, "AsyncOlostep", side_effect=AssertionError("Demo created SDK")):
            app = StockSignal(demo=True)
            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.press("r")
                await app.workers.wait_for_complete()
                self.assertEqual(len(app.results), 3)
                self.assertTrue(all(result["status"] == "DEMO" for result in app.results.values()))


class LayoutTests(unittest.TestCase):
    def test_question_contracts(self):
        notebook = json.loads((Path(__file__).resolve().parents[1] / "notebooks/stocksignal_simple.ipynb").read_text(encoding="utf-8"))
        cell = next(cell for cell in notebook["cells"] if cell["id"] == "questions")
        namespace = {"Choice": jev.Choice, "Score": jev.Score}
        exec("".join(cell["source"]), namespace)
        app = jev.analysis_input({}, {})["questions"]
        script = simple.request_body({}, {})["questions"]
        self.assertEqual(set(namespace["questions"]), set(app))
        self.assertEqual(set(namespace["questions"]["signal"].criteria), set(jev.SIGNALS))
        self.assertEqual(len(namespace["questions"]["impact"].criteria), 5)
        for name, question in app.items():
            self.assertEqual(script[name]["instructions"], question.instructions)
            self.assertEqual(script[name]["criteria"], question.criteria)

    def test_script_and_notebook_have_no_project_imports(self):
        root = Path(__file__).resolve().parents[1]
        notebook = json.loads((root / "notebooks" / "stocksignal_simple.ipynb").read_text(encoding="utf-8"))
        sources = [(root / "examples" / "simple.py").read_text(encoding="utf-8")]
        sources += ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]
        for source in sources:
            for node in ast.walk(ast.parse(source)):
                modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                           else [node.module] if isinstance(node, ast.ImportFrom) else [])
                self.assertFalse({name.split(".")[0] for name in modules} & {"stocksignal", "app", "market", "jev", "simple"})


class NotebookTests(unittest.IsolatedAsyncioTestCase):
    def cell(self, name):
        notebook = json.loads((Path(__file__).resolve().parents[1] / "notebooks/stocksignal_simple.ipynb").read_text(encoding="utf-8"))
        return "".join(next(cell["source"] for cell in notebook["cells"] if cell["id"] == name))

    def test_tickers_are_validated_before_requests(self):
        for text in ("", "NVDA NVDA", "A B C D E F", "NVDA/", "NVDA $AAPL"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                source = self.cell("settings")
                source = source.replace(source.splitlines()[0], f"TICKERS = {text!r}", 1)
                exec(source, {"re": simple.re})

    async def test_sdk_news_deduplication_filtering_and_empty_results(self):
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
        client = AsyncMock()
        client.__aenter__.return_value = client
        links = [
            {"url": "https://news.example/story?utm_source=x", "title": "News", "markdown_content": "Extracted text"},
            {"url": "https://news.example/story#top", "title": "Duplicate URL", "description": "Snippet"},
            {"url": "https://other.example/story", "title": "News", "description": "Duplicate title"},
            {"url": "https://www.facebook.com/post", "title": "Social", "description": "Exclude"},
            {"url": "https://www.cnbc.com/quotes/NVDA", "title": "Quote", "description": "Exclude"},
            {"url": "https://www.msn.com/en-us/money/stockdetails/nvda", "title": "Stock directory", "description": "Exclude"},
        ]
        namespace = {"os": os, "re": simple.re, "datetime": simple.datetime,
                     "timezone": simple.timezone, "timedelta": simple.timedelta,
                     "urlsplit": urlsplit, "urlunsplit": urlunsplit, "urlencode": urlencode, "parse_qsl": parse_qsl,
                     "AsyncOlostep": lambda **kwargs: client, "tickers": ["NVDA"],
                     "market": {"NVDA": {"company": "NVIDIA"}}}
        for source in (links, []):
            client.searches.create.return_value = SimpleNamespace(links=source)
            with patch.dict(os.environ, {"OLOSTEP_API_KEY": "test-key"}), patch("builtins.print"):
                await eval(compile(self.cell("news"), "<notebook>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), namespace)
            articles = namespace["news"]["NVDA"]["articles"]
            self.assertEqual(len(articles), 1 if source else 0)
            if source:
                self.assertEqual(articles[0]["url"], "https://news.example/story")
                self.assertEqual(articles[0]["excerpt"], "Extracted text")
                self.assertEqual(articles[0]["content_basis"], "Extracted article")

    async def test_native_sdk_decision_values_and_invalid_responses(self):
        import math
        from time import perf_counter
        for probabilities, choice, score, valid in (
            ({"BUY": .72, "HOLD": .2, "SELL": .08}, "BUY", 2.7, True),
            ({"BUY": .72, "HOLD": .2}, "BUY", 2.7, False),
            ({"BUY": .5, "HOLD": .2, "SELL": .08}, "BUY", 2.7, False),
            ({"BUY": .72, "HOLD": .2, "SELL": .08}, "HOLD", 2.7, False),
            ({"BUY": .72, "HOLD": .2, "SELL": .08}, "BUY", float("nan"), False),
        ):
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.system_one.return_value = SimpleNamespace(
                choices={"signal": SimpleNamespace(choice=choice, probabilities=probabilities, confidence=.5)},
                scores={"impact": SimpleNamespace(score=score)})
            namespace = {"math": math, "os": os, "perf_counter": perf_counter,
                         "AsyncTypeSafeClient": lambda **kwargs: client,
                         "tickers": ["NVDA"], "market": {"NVDA": {"price": 100}},
                         "news": {"NVDA": {"articles": []}}, "questions": {}}
            with self.subTest(probabilities=probabilities, choice=choice, score=score), patch.dict(os.environ, {"JEV_API_KEY": "test-key"}):
                execution = eval(compile(self.cell("decisions"), "<notebook>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), namespace)
                if valid:
                    await execution
                    result = namespace["results"]["NVDA"]
                    self.assertEqual(result["confidence_percent"], 72)
                    self.assertEqual(result["market_impact"], 4)
                    self.assertAlmostEqual(sum(result["probabilities_percent"].values()), 100)
                    self.assertEqual(client.system_one.call_args.kwargs["state"]["prediction_horizon_trading_days"], 5)
                else:
                    with self.assertRaises(ValueError):
                        await execution
                    self.assertEqual(namespace["results"], {})



class SimpleTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_is_offline_and_supports_five_stocks(self):
        with patch.object(simple, "fetch_market", new=AsyncMock(side_effect=AssertionError("Demo fetched market"))) as fetch, \
             patch.object(simple, "fetch_news", new=AsyncMock(side_effect=AssertionError("Demo fetched news"))) as news, \
             patch.object(simple, "decide", new=AsyncMock(side_effect=AssertionError("Demo requested Jev"))) as analyze:
            results = await analyze_tickers("NVDA AAPL MSFT TSLA AMZN", demo=True)
        self.assertEqual(len(results), 5)
        self.assertEqual(results["NVDA"]["decision"]["signal"], "BUY")
        self.assertEqual(results["AAPL"]["decision"]["signal"], "HOLD")
        self.assertEqual(results["MSFT"]["decision"]["signal"], "SELL")
        self.assertTrue(all(result["status"] == "DEMO" for result in results.values()))
        fetch.assert_not_awaited()
        news.assert_not_awaited()
        analyze.assert_not_awaited()
        with self.assertRaises(ValueError):
            await analyze_tickers("NVDA NVDA", demo=True)
        with self.assertRaises(ValueError):
            await analyze_tickers("SPXC", demo=True)

    async def test_live_failure_isolated_without_sample_fallback(self):
        async def fetch(ticker):
            if ticker == "MU":
                raise ValueError("MU: market unavailable")
            return demo_result(ticker)["market"]
        with patch.dict(os.environ, {"OLOSTEP_API_KEY": "test", "JEV_API_KEY": "test"}), \
             patch.object(simple, "fetch_market", side_effect=fetch), \
             patch.object(simple, "fetch_news", new=AsyncMock(return_value={"articles": [], "status": "No news", "date_status": "Unverified"})), \
             patch.object(simple, "decide", new=AsyncMock(return_value=simple.parse_decision(decision_payload()))):
            results = await analyze_tickers("NVDA MU")
        self.assertEqual(results["NVDA"]["status"], "Done")
        self.assertEqual(results["MU"]["status"], "Error")
        self.assertIsNone(results["MU"]["decision"])
        self.assertIsNone(results["MU"]["market"])
        self.assertIsNotNone(results["MU"]["elapsed_seconds"])

    async def test_missing_keys_prevents_requests(self):
        with patch.dict(os.environ, {"OLOSTEP_API_KEY": "", "JEV_API_KEY": ""}), \
             patch.object(simple, "fetch_market", new=AsyncMock()) as fetch:
            with self.assertRaisesRegex(ValueError, "Set OLOSTEP_API_KEY, JEV_API_KEY"):
                await analyze_tickers("NVDA")
        fetch.assert_not_awaited()

    async def test_standalone_api_contract_and_invalid_probabilities(self):
        requests = []
        def respond(request):
            requests.append(request)
            if request.url.host == "api.typesafe.ai":
                return httpx.Response(200, json=decision_payload())
            return httpx.Response(200, json={"result": {"links": [
                {"url": "https://news.example/story?utm_source=x", "title": "Company announcement", "markdown_content": "Article content"},
                {"url": "https://news.example/story#top", "title": "Duplicate", "description": "Duplicate URL"},
                {"url": "https://www.facebook.com/post", "title": "Social", "description": "Exclude"},
                {"url": "https://www.cnbc.com/quotes/NVDA", "title": "Quote", "description": "Exclude"},
            ]}})
        with patch.dict(os.environ, {"OLOSTEP_API_KEY": "test-olo", "JEV_API_KEY": "test-jev"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                news = await simple.fetch_news(client, simple.demo_result("NVDA")["market"])
                decision = await simple.decide(client, simple.demo_result("NVDA")["market"], news)
        self.assertEqual(len(news["articles"]), 1)
        self.assertEqual(news["articles"][0]["url"], "https://news.example/story")
        self.assertEqual(decision["confidence"], 72)
        self.assertEqual(decision["impact"], 4)
        self.assertGreaterEqual(decision["elapsed_seconds"], 0)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-olo")
        self.assertEqual(requests[1].headers["Authorization"], "Bearer test-jev")
        self.assertEqual(json.loads(requests[1].content)["state"]["prediction_horizon_trading_days"], 5)
        payload = decision_payload()
        payload["answers"]["signal"]["probabilities"].pop("SELL")
        with self.assertRaises(ValueError):
            simple.parse_decision(payload)

    async def test_standalone_market_calculations(self):
        history = pd.DataFrame({"Close": range(100, 122), "Adj Close": range(100, 122)},
                               index=pd.date_range("2026-09-01", periods=22, freq="B"))
        with patch.object(simple.yf, "Ticker") as ticker:
            ticker.return_value.history.return_value = history
            ticker.return_value.get_info.return_value = {"quoteType": "EQUITY", "currency": "USD"}
            result = await simple.fetch_market("NVDA")
            self.assertEqual(result["price"], 121)
            self.assertAlmostEqual(result["return_5_sessions_percent"], (121 / 116 - 1) * 100)
            self.assertIsNone(result["market_cap"])
            ticker.return_value.get_info.return_value["quoteType"] = "ETF"
            with self.assertRaises(ValueError):
                await simple.fetch_market("SPY")


class UiTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_navigation_add_delete_refresh(self):
        with patch.object(market, "fetch_market", new=AsyncMock(side_effect=AssertionError("Demo accessed Yahoo"))):
            app = StockSignal(demo=True)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                self.assertEqual(app.query_one(DataTable).row_count, 3)
                await pilot.press("down")
                await pilot.pause()
                self.assertEqual(app.selected, "AAPL")
                await pilot.press("enter")
                self.assertEqual(app.focused.id, "details")
                await pilot.press("escape", "a")
                app.query_one(Input).value = "TSLA AMZN"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(len(app.results), 5)
                self.assertEqual(app.selected, "AAPL")
                await pilot.press("r")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertTrue(all(r["status"] == "DEMO" for r in app.results.values()))
                self.assertTrue(all(r["elapsed_seconds"] is not None for r in app.results.values()))
                summary = str(app.query_one("#run-summary", Static).render())
                self.assertIn("Last run:", summary)
                self.assertNotIn("cost", summary.lower())
                self.assertNotIn("credits", summary.lower())
                self.assertIn("DEMO: no API calls", summary)
                self.assertFalse(app.busy)
                self.assertEqual(app.selected, "AAPL")
                await pilot.press("d")
                await pilot.pause()
                self.assertEqual(len(app.results), 4)
                self.assertIn("DEMO", str(app.query_one("#mode", Static).render()))

    async def test_failure_isolated_and_no_stale_decision(self):
        async def news(client, data):
            if data["ticker"] == "AAPL":
                raise ValueError("Olostep HTTP 401")
            return {"status": "1 source retrieved", "date_status": "Unverified dates", "credits_consumed": 6, "articles": [
                {"title": "Company announcement", "url": "https://example.com/news", "content_basis": "Scraped article excerpt"}]}
        with patch.dict(os.environ, {"JEV_API_KEY": "test", "OLOSTEP_API_KEY": "test"}), \
             patch.object(market, "fetch_market", new=AsyncMock(side_effect=lambda t: copy.deepcopy(demo_result(t)["market"]))), \
             patch.object(olostep, "fetch_news", side_effect=news), \
             patch.object(jev, "analyze", new=AsyncMock(return_value=jev.parse_decision(decision_payload()))):
            app = StockSignal("NVDA AAPL")
            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                app.results["AAPL"]["decision"] = demo_result("AAPL")["decision"]
                await pilot.press("r")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.results["NVDA"]["status"], "Done")
                self.assertEqual(app.results["AAPL"]["status"], "Error")
                self.assertIsNone(app.results["AAPL"]["decision"])
                self.assertEqual(app.results["AAPL"]["error"], "Olostep HTTP 401")
                self.assertIsNotNone(app.results["AAPL"]["elapsed_seconds"])
                summary = str(app.query_one("#run-summary", Static).render())
                self.assertIn("Last run:", summary)
                app.selected = "NVDA"
                await app.render_details()
                self.assertEqual(len(app.query(Link)), 1)
                with patch.object(app, "open_url") as open_url:
                    app.query_one(Link).focus()
                    await pilot.press("enter")
                    open_url.assert_called_once_with("https://example.com/news")

    async def test_duplicate_visible(self):
        app = StockSignal(demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("a")
            app.query_one(Input).value = "nvda"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            self.assertIn("Duplicate", str(app.query_one("#status", Static).render()))
            self.assertEqual(len(app.results), 3)

    async def test_ui_responsive_during_requests(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_market(ticker):
            started.set()
            await release.wait()
            return demo_result(ticker)["market"]
        with patch.dict(os.environ, {"JEV_API_KEY": "test", "OLOSTEP_API_KEY": "test"}), \
             patch.object(market, "fetch_market", side_effect=slow_market), \
             patch.object(olostep, "fetch_news", new=AsyncMock(return_value={"status": "No news", "date_status": "Unverified", "articles": []})), \
             patch.object(jev, "analyze", new=AsyncMock(return_value=jev.parse_decision(decision_payload()))):
            app = StockSignal()
            async with app.run_test(size=(100, 32)) as pilot:
                app.results = {t: demo_result(t) for t in ("NVDA", "AAPL")}
                app.selected = "NVDA"
                app.render_table()
                app.action_table()
                app.action_refresh()
                await asyncio.wait_for(started.wait(), 2)
                self.assertTrue(app.busy)
                self.assertTrue(app.query_one(Input).disabled)
                self.assertIsNone(app.results["NVDA"]["decision"])
                self.assertIsNone(app.results["NVDA"]["elapsed_seconds"])
                self.assertIn("running", str(app.query_one("#run-summary", Static).render()))
                await pilot.press("down", "d")
                self.assertEqual(app.selected, "AAPL")
                self.assertEqual(len(app.results), 2)
                release.set()
                await app.workers.wait_for_complete()
                self.assertFalse(app.busy)


if __name__ == "__main__":
    unittest.main()
