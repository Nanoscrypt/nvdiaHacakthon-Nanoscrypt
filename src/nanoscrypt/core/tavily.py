"""First-party Tavily search support used by the hackathon build."""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

KEYRING_SERVICE = "nanoscrypt"
KEYRING_USERNAME = "tavily_api_key"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"

_SEARCH_MARKERS = (
    "search the web",
    "search web",
    "web search",
    "browse the web",
    "browse web",
    "search online",
    "look up online",
    "look online",
    "find online",
    "search the internet",
    "search internet",
    "internet search",
    "google ",
)
_FRESHNESS_MARKERS = (
    "latest news",
    "latest updates",
    "current news",
    "today's news",
    "today's news",
    "news today",
    "what happened today",
    "recent news",
    "current temperature",
    "current weather",
    "weather today",
    "forecast today",
)


class TavilySearchError(RuntimeError):
    """A safe, user-facing Tavily API error with no credential details."""


def is_web_search_request(prompt: str) -> bool:
    text = prompt.casefold()
    if any(marker in text for marker in _SEARCH_MARKERS) or any(
        marker in text for marker in _FRESHNESS_MARKERS
    ):
        return True

    # Weather and temperature lookups are time-sensitive even when the user
    # doesn't explicitly say "current". Route location-based requests to the
    # configured live-search provider instead of letting the model guess.
    asks_weather = re.search(r"\b(weather|temperatures?|forecast)\b", text)
    has_place = re.search(r"\b(in|for|near|at)\s+[a-z][\w-]*", text)
    asks_for_current_conditions = re.search(
        r"\b(current|today|now|tonight|tomorrow|right now|latest)\b", text
    )
    is_explanatory_weather_question = re.match(
        r"\s*(how|why|explain|describe)\b", text
    )
    if (
        asks_weather
        and (has_place or asks_for_current_conditions)
        and not is_explanatory_weather_question
    ):
        return True

    local_search_context = re.search(
        r"\b(in|inside|within)\s+(the\s+)?(repo|repository|codebase|workspace|file|folder|directory)\b",
        text,
    )
    if not local_search_context and re.search(r"\b(search for|look up)\b", text):
        return True

    asks_for_freshness = re.search(r"\b(latest|current|today|recent|right now)\b", text)
    asks_for_fresh_topic = re.search(
        r"\b(news|updates?|AI|models?|prices?|releases?|weather|temperatures?|forecast|conditions|scores?|events?)\b",
        text,
    )
    return bool(asks_for_freshness and asks_for_fresh_topic)


def extract_search_query(prompt: str) -> str:
    text = prompt.strip()
    prefix = re.compile(
        r"^(?:(?:please|can you|could you)\s+)?"
        r"(?:(?:do|perform|run)\s+)?"
        r"(?:a\s+)?"
        r"(?:(?:web|internet|online)\s+)?search"
        r"(?:\s+(?:the\s+)?(?:web|internet|online))?"
        r"(?:\s+(?:for|about|on))?\s*[:,-]?\s*",
        re.IGNORECASE,
    )
    text = prefix.sub("", text, count=1)
    text = re.sub(
        r"^(?:(?:please|can you|could you)\s+)?browse\s+"
        r"(?:the\s+)?(?:web|internet|online)"
        r"(?:\s+(?:for|about|on))?\s*[:,-]?\s*",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^(?:(?:please|can you|could you)\s+)?(?:look\s+up|look|find)\s+"
        r"(?:this\s+)?(?:online|on the web|on the internet)"
        r"(?:\s+(?:for|about|on))?\s*[:,-]?\s*",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^(?:(?:please|can you|could you)\s+)?(?:search for|look up)\s*[:,-]?\s*",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    return text.strip(" \t\r\n?.!") or prompt.strip()


def get_stored_tavily_api_key() -> str | None:
    """Read Tavily credentials from the environment or OS credential store."""
    key = os.getenv("TAVILY_API_KEY")
    if key:
        return key.strip() or None

    try:
        import keyring

        backend = keyring.get_keyring()
        if getattr(backend, "priority", 0) <= 0:
            return None
        value = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        return value.strip() if value and value.strip() else None
    except Exception:
        return None


def store_tavily_api_key(api_key: str) -> bool:
    """Store the credential in the configured OS keyring; never use a file fallback."""
    try:
        import keyring

        backend = keyring.get_keyring()
        if getattr(backend, "priority", 0) <= 0:
            return False
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, api_key)
        return True
    except Exception:
        return False


def search_tavily(api_key: str, query: str, max_results: int = 6) -> dict[str, Any]:
    """Call Tavily Search and return decoded JSON, with safe API error messages."""
    payload = json.dumps(
        {
            "query": query,
            "search_depth": "basic",
            "max_results": max(1, min(int(max_results), 10)),
            "include_answer": False,
            "include_raw_content": False,
        }
    ).encode("utf-8")
    request = Request(
        TAVILY_SEARCH_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 401:
            raise TavilySearchError(
                "Tavily rejected the API key (HTTP 401). Check TAVILY_API_KEY."
            ) from None
        if exc.code == 429:
            raise TavilySearchError(
                "Tavily rate limit reached (HTTP 429). Try again later."
            ) from None
        if exc.code in (432, 433):
            raise TavilySearchError(
                "Tavily reports that the account usage limit was reached."
            ) from None
        raise TavilySearchError(f"Tavily search failed with HTTP {exc.code}.") from None
    except (TimeoutError, URLError) as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            raise TavilySearchError(
                "Tavily search timed out after 30 seconds."
            ) from None
        raise TavilySearchError("Could not connect to the Tavily Search API.") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise TavilySearchError("Tavily returned an invalid response.") from None

    if not isinstance(data, dict):
        raise TavilySearchError("Tavily returned an unexpected response format.")
    results = data.get("results", [])
    if not isinstance(results, list):
        raise TavilySearchError("Tavily returned an unexpected results format.")
    return data
