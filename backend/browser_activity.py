from __future__ import annotations

import re
from dataclasses import dataclass

_BROWSER_NAMES = {
    "chrome.exe": "Chrome",
    "msedge.exe": "Microsoft Edge",
    "firefox.exe": "Firefox",
}

_BROWSER_NAME_TOKENS = {
    "google chrome",
    "chrome",
    "microsoft edge",
    "edge",
    "mozilla firefox",
    "firefox",
}

_KNOWN_SITE_MAPPINGS: list[tuple[tuple[str, ...], tuple[str, str, str]]] = [
    (("chatgpt",), ("ChatGPT", "coding", "chatgpt")),
    (("claude",), ("Claude", "coding", "claude")),
    (("github",), ("GitHub", "coding", "github")),
    (("stack overflow", "stackoverflow"), ("Stack Overflow", "coding", "stackoverflow")),
    (("coursera", "udemy", "edx"), ("Online Learning", "studying", "online-learning")),
    (("mangadex", "manhwa", "manga"), ("Manga Reading", "browsing", "manga")),
    (("docs", "documentation", "readthedocs", "developer.mozilla", "mdn"), ("Documentation", "coding", "documentation")),
]

_IDLE_YOUTUBE_TITLES = {
    "youtube",
    "home - youtube",
    "youtube home",
    "home",
}


@dataclass(frozen=True)
class BrowserActivity:
    activity_name: str
    browser_name: str
    category: str
    site_name: str
    video_title: str = ""
    is_youtube: bool = False
    site_known: bool = False

    @property
    def timeline_label(self) -> str:
        return f"{self.activity_name} ({self.browser_name})"


def is_browser_process(process_name: str) -> bool:
    return process_name.lower().strip() in _BROWSER_NAMES


def parse_browser_activity(process_name: str, window_title: str) -> BrowserActivity | None:
    proc = process_name.lower().strip()
    if proc not in _BROWSER_NAMES:
        return None

    browser_name = _BROWSER_NAMES[proc]
    title = (window_title or "").strip()
    lowered = title.lower()

    if "youtube" in lowered:
        video_title = _extract_youtube_video_title(title)
        if video_title:
            return BrowserActivity(
                activity_name=f"YouTube: {video_title}",
                browser_name=browser_name,
                category="youtube",
                site_name="youtube",
                video_title=video_title,
                is_youtube=True,
                site_known=True,
            )
        return BrowserActivity(
            activity_name="YouTube",
            browser_name=browser_name,
            category="youtube",
            site_name="youtube",
            video_title="",
            is_youtube=True,
            site_known=True,
        )

    site_name = _guess_site_name(title)
    site_key = site_name.lower()
    for needles, payload in _KNOWN_SITE_MAPPINGS:
        if any(needle in lowered or needle in site_key for needle in needles):
            activity_name, category, canonical_site = payload
            return BrowserActivity(
                activity_name=activity_name,
                browser_name=browser_name,
                category=category,
                site_name=canonical_site,
                site_known=True,
            )

    activity_name = _normalize_activity_name(site_name)
    return BrowserActivity(
        activity_name=activity_name,
        browser_name=browser_name,
        category="browsing",
        site_name=site_key,
        site_known=False,
    )


def _guess_site_name(window_title: str) -> str:
    if not window_title.strip():
        return "Browser"

    segments = [segment.strip() for segment in window_title.split(" - ") if segment.strip()]
    for segment in reversed(segments):
        cleaned = segment.lower().strip()
        if cleaned in _BROWSER_NAME_TOKENS:
            continue
        if cleaned.endswith("chrome") and cleaned != "chrome":
            continue
        if cleaned.endswith("edge") and cleaned != "edge":
            continue
        if cleaned.endswith("firefox") and cleaned != "firefox":
            continue
        return segment

    return segments[0] if segments else "Browser"


def _extract_youtube_video_title(window_title: str) -> str:
    title = (window_title or "").strip()
    lowered = title.lower()
    if lowered in _IDLE_YOUTUBE_TITLES:
        return ""

    segments = [segment.strip() for segment in title.split(" - ") if segment.strip()]
    segments = [segment for segment in segments if segment.lower() not in _BROWSER_NAME_TOKENS]
    if not segments:
        return ""

    yt_index = next((idx for idx, value in enumerate(segments) if "youtube" in value.lower()), -1)
    if yt_index <= 0:
        return ""

    candidate = segments[0].strip()
    if not candidate or candidate.lower() in _IDLE_YOUTUBE_TITLES:
        return ""
    return candidate


def _normalize_activity_name(site_name: str) -> str:
    text = re.sub(r"\s+", " ", site_name).strip()
    if not text:
        return "Web Browsing"
    if text.lower() == "browser":
        return "Web Browsing"
    if len(text) > 42:
        text = text[:42].rsplit(" ", 1)[0].strip() or text[:42]
    return text.title()
