from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from groq_client import GroqClient

logger = logging.getLogger(__name__)

_ALLOWED_YT_CATEGORIES = {
    "music",
    "tutorial",
    "gameplay",
    "education",
    "documentary",
    "podcast",
    "entertainment",
}

_BROWSER_TITLE_SUFFIXES = {
    "google chrome",
    "chrome",
    "microsoft edge",
    "edge",
    "mozilla firefox",
    "firefox",
}

_IDLE_YOUTUBE_TITLES = {
    "youtube",
    "home - youtube",
    "youtube home",
    "home",
}

_KNOWN_SITE_HINTS: dict[str, tuple[str, str, str]] = {
    "mangadex": ("Manga Reading", "browsing", "manga reading"),
    "coursera": ("Online Learning", "studying", "online learning"),
    "stackoverflow": ("Programming Help", "coding", "programming help"),
    "github": ("Development", "coding", "software development"),
    "chatgpt": ("AI Assistance", "coding", "ai assistance"),
    "claude": ("AI Assistance", "coding", "ai assistance"),
    "notion": ("Note Taking", "documents", "productivity docs"),
    "drive": ("Cloud Documents", "documents", "cloud storage"),
}


@dataclass(frozen=True)
class WebsiteClassification:
    activity_name: str
    category: str
    activity_type: str
    source: str


class YouTubeClassifier:
    """YouTube and unknown website classifier with Groq + deterministic fallback."""

    def __init__(self, groq_client: "GroqClient | None" = None) -> None:
        self.groq_client = groq_client
        self._youtube_cache: dict[str, str] = {}
        self._site_cache: dict[str, WebsiteClassification] = {}

    def extract_video_title(self, window_title: str) -> str:
        title = (window_title or "").strip()
        lowered = title.lower()
        if "youtube" not in lowered:
            return ""
        if lowered in _IDLE_YOUTUBE_TITLES:
            return ""

        segments = [segment.strip() for segment in title.split(" - ") if segment.strip()]
        if not segments:
            return ""

        filtered = [segment for segment in segments if segment.lower() not in _BROWSER_TITLE_SUFFIXES]
        if not filtered:
            return ""

        yt_index = next((idx for idx, part in enumerate(filtered) if "youtube" in part.lower()), -1)
        if yt_index == 0:
            return ""
        if yt_index > 0:
            candidate = filtered[0]
        else:
            candidate = filtered[0]

        candidate = candidate.strip()
        if not candidate:
            return ""
        if candidate.lower() in _IDLE_YOUTUBE_TITLES:
            return ""
        if len(candidate) < 4:
            return ""
        return candidate

    def classify_youtube_video(self, video_title: str) -> str:
        title = (video_title or "").strip()
        if not title:
            return "entertainment"
        cache_key = title.lower()
        cached = self._youtube_cache.get(cache_key)
        if cached:
            return cached

        if self.groq_client is not None and self.groq_client.enabled and self.groq_client.client is not None:
            try:
                completion = self.groq_client.client.chat.completions.create(
                    model=self.groq_client.query_model,
                    temperature=0.0,
                    max_tokens=16,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Classify the YouTube video category from the title. "
                                "Allowed categories: music, tutorial, gameplay, education, "
                                "documentary, podcast, entertainment. "
                                "Return exactly one allowed category."
                            ),
                        },
                        {"role": "user", "content": title},
                    ],
                )
                content = (completion.choices[0].message.content or "").strip().lower()
                candidate = re.sub(r"[^a-z]", "", content.splitlines()[0].strip())
                if candidate in _ALLOWED_YT_CATEGORIES:
                    self._youtube_cache[cache_key] = candidate
                    return candidate
            except Exception:
                logger.debug("Groq YouTube classification failed, using fallback.", exc_info=True)

        fallback = self._fallback_youtube_category(title)
        self._youtube_cache[cache_key] = fallback
        return fallback

    def classify_unknown_website(self, site_name: str, window_title: str = "") -> WebsiteClassification:
        site = (site_name or "").strip()
        lowered_site = site.lower()
        combined = f"{site} {window_title}".strip()
        combined_lower = combined.lower()
        cache_key = (lowered_site or combined_lower)[:180]
        if cache_key:
            cached = self._site_cache.get(cache_key)
            if cached is not None:
                return cached

        for key, value in _KNOWN_SITE_HINTS.items():
            if key in lowered_site or key in combined_lower:
                name, category, activity_type = value
                result = WebsiteClassification(
                    activity_name=name,
                    category=category,
                    activity_type=activity_type,
                    source="heuristic",
                )
                if cache_key:
                    self._site_cache[cache_key] = result
                return result

        if self.groq_client is not None and self.groq_client.enabled and self.groq_client.client is not None:
            try:
                completion = self.groq_client.client.chat.completions.create(
                    model=self.groq_client.query_model,
                    temperature=0.1,
                    response_format={"type": "json_object"},
                    max_tokens=120,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You classify unknown websites for a personal memory assistant. "
                                "Return strict JSON with keys: activity_name, activity_type, category. "
                                "category must be one of: coding, browsing, studying, communication, "
                                "documents, gaming, other."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Site: {site}\n"
                                f"Tab title: {window_title}\n"
                                "What type of website is this used for?"
                            ),
                        },
                    ],
                )
                payload = self._parse_json(completion.choices[0].message.content or "{}")
                category = str(payload.get("category") or "browsing").strip().lower()
                if category not in {"coding", "browsing", "studying", "communication", "documents", "gaming", "other"}:
                    category = "browsing"
                activity_type = str(payload.get("activity_type") or "").strip().lower() or "website browsing"
                activity_name = str(payload.get("activity_name") or "").strip()
                if not activity_name:
                    activity_name = self._title_case_site(site) or "Web Browsing"
                result = WebsiteClassification(
                    activity_name=activity_name,
                    category=category,
                    activity_type=activity_type,
                    source="groq",
                )
                if cache_key:
                    self._site_cache[cache_key] = result
                return result
            except Exception:
                logger.debug("Groq website classification failed, using fallback.", exc_info=True)

        fallback = self._fallback_unknown_site(site, combined_lower)
        if cache_key:
            self._site_cache[cache_key] = fallback
        return fallback

    def _fallback_youtube_category(self, title: str) -> str:
        t = title.lower()
        if any(token in t for token in ("gameplay", "walkthrough", "let's play", "boss fight", "trailer")):
            return "gameplay"
        if any(token in t for token in ("tutorial", "how to", "guide", "setup", "install", "explained")):
            return "tutorial"
        if any(token in t for token in ("lecture", "course", "university", "research", "study", "encryption", "cryptography")):
            return "education"
        if any(token in t for token in ("documentary", "history of", "behind the scenes", "investigation")):
            return "documentary"
        if any(token in t for token in ("podcast", "episode", "interview", "roundtable")):
            return "podcast"
        if any(token in t for token in ("music", "song", "beats", "lofi", "lo-fi", "mix", "playlist")):
            return "music"
        return "entertainment"

    def _fallback_unknown_site(self, site: str, combined_lower: str) -> WebsiteClassification:
        if any(token in combined_lower for token in ("docs", "documentation", "readthedocs", "developer.mozilla", "mdn")):
            return WebsiteClassification("Documentation", "coding", "documentation", "heuristic")
        if any(token in combined_lower for token in ("learn", "course", "lecture", "academy", "study", "tutorial")):
            return WebsiteClassification("Online Learning", "studying", "online learning", "heuristic")
        if any(token in combined_lower for token in ("forum", "reddit", "discord", "community")):
            return WebsiteClassification("Community", "communication", "community discussion", "heuristic")
        if any(token in combined_lower for token in ("shop", "store", "buy", "cart")):
            return WebsiteClassification("Shopping", "browsing", "shopping", "heuristic")
        if any(token in combined_lower for token in ("news", "times", "post", "guardian", "bbc")):
            return WebsiteClassification("News Reading", "browsing", "news reading", "heuristic")
        if any(token in combined_lower for token in ("manga", "chapter", "manhwa")):
            return WebsiteClassification("Manga Reading", "browsing", "manga reading", "heuristic")
        return WebsiteClassification(
            activity_name=self._title_case_site(site) or "Web Browsing",
            category="browsing",
            activity_type="website browsing",
            source="heuristic",
        )

    def _title_case_site(self, site: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9.\- ]+", " ", site).strip()
        if not cleaned:
            return ""
        return re.sub(r"\s+", " ", cleaned).title()

    def _parse_json(self, payload: str) -> dict[str, object]:
        try:
            loaded = json.loads(payload)
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            match = re.search(r"\{.*\}", payload, flags=re.DOTALL)
            if not match:
                return {}
            try:
                loaded = json.loads(match.group(0))
                return loaded if isinstance(loaded, dict) else {}
            except Exception:
                return {}
