"""Olostep search with embedded article text; no generated news summaries."""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from olostep import AsyncOlostep, RetryStrategy, errors

SOCIAL_DOMAINS = ("facebook.com", "instagram.com", "reddit.com", "x.com", "twitter.com")


def news_from_response(payload: dict) -> list[dict]:
    try:
        links = payload["result"]["links"]
        if not isinstance(links, list):
            raise ValueError
        articles, urls, titles = [], set(), []
        for link in links:
            parts = urlsplit(link["url"])
            if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
                continue
            # Server-side domain exclusions returned empty searches despite available articles.
            if any(parts.hostname == domain or parts.hostname.endswith("." + domain) for domain in SOCIAL_DOMAINS):
                continue
            if re.search(r"/(quotes?|symbol|financial-reports|quarterly-results|events-and-presentations|earnings-history|earnings-calendar|future|forecast)(/|$)", parts.path):
                continue
            if re.fullmatch(r"/stock/[^/]+/?", parts.path):
                continue
            if parts.path.rstrip("/") in ("", "/home/default.aspx"):
                continue
            query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
            url = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))
            title = link.get("title") or parts.hostname
            normalized = re.sub(r"\W+", " ", title.lower()).strip()
            if url in urls or any(SequenceMatcher(None, normalized, t).ratio() > 0.9 for t in titles):
                continue
            content = link.get("markdown_content") or ""
            description = link.get("description") or ""
            if not isinstance(content, str) or not isinstance(description, str) or not isinstance(title, str):
                raise ValueError
            if not content.strip() and not description.strip():
                continue
            urls.add(url)
            titles.append(normalized)
            # shortcut: bounded article excerpts and headline deduplication; upgrade for full-news coverage.
            articles.append({"title": title, "url": url, "excerpt": (description + "\n\n" + content).strip()[:10000],
                             "content_basis": "Scraped article excerpt" if content else "Search snippet only",
                             "publication_date": None})
        return articles
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError("Olostep returned an invalid news response.") from None


async def fetch_news(client: AsyncOlostep, market: dict) -> dict:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=7)
    before = end + timedelta(days=1)
    query = (
        f'("{market["company"]}" OR "{market["ticker"]}") '
        '(earnings OR guidance OR announcement OR filing OR financial OR acquisition) '
        f'after:{start.isoformat()} before:{before.isoformat()} '
        '-inurl:quote -inurl:financial-reports -inurl:quarterly-results -inurl:events-and-presentations'
    )
    try:
        async with asyncio.timeout(60):
            response = await client.searches.create(
                query=query, limit=5, scrape_options={"formats": ["markdown"], "timeout": 25},
            )
        articles = news_from_response({"result": {"links": response.links}})
    except (TimeoutError, errors.OlostepClientError_Timeout, errors.OlostepServerError_NoResultInResponse):
        raise ValueError("Olostep timed out. Retry the analysis.") from None
    except errors.Olostep_APIConnectionError:
        raise ValueError("Olostep connection failed. Check network access.") from None
    except errors.OlostepServerError_AuthFailed:
        raise ValueError("Olostep authentication failed. Check OLOSTEP_API_KEY.") from None
    except errors.OlostepServerError_CreditsExhausted:
        raise ValueError("Olostep credits exhausted. Check your account.") from None
    except (json.JSONDecodeError, errors.OlostepClientError_ResponseValidationFailed, TypeError, KeyError):
        raise ValueError("Olostep returned an invalid news response.") from None
    except errors.OlostepServerError_UnknownIssue as error:
        if error.status_code >= 400:
            raise ValueError(f"Olostep HTTP {error.status_code}. Check API key/credits or retry.") from None
        raise ValueError("Olostep returned an invalid news response.") from None
    except errors.Olostep_BaseError:
        raise ValueError("Olostep request failed. Check API key/credits or retry.") from None
    return {
        "articles": articles,
        "status": f"{len(articles)} sources retrieved" if articles else "No relevant news retrieved for the requested 7-day window.",
        "requested_window": {"start": start.isoformat(), "end": end.isoformat()},
        "date_status": "7-day window requested; publication dates are not provided by the search API and are unverified.",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
