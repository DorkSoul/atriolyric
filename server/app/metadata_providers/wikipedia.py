"""Artist bios from Wikipedia, reached through MusicBrainz's links: either a
direct Wikipedia URL or a Wikidata entity whose English sitelink names the
article. Also artist photos: the Wikidata entity's image (P18) on Wikimedia
Commons, with the author and licence its reuse terms ask to show."""

import html
import logging
import re
from urllib.parse import quote, unquote, urlparse

import httpx

from app.config import settings
from app.metadata_providers._throttle import Throttle
from app.metadata_providers.result import (
    NOT_FOUND,
    Failed,
    Found,
    ProviderResult,
    failed,
    retry_after_seconds,
)

_logger = logging.getLogger(__name__)
_throttle = Throttle(0.2)


def _get_json(url: str, params: dict | None = None) -> ProviderResult[dict]:
    if not _throttle.wait():
        return failed("Wikipedia is busy - try again shortly")
    try:
        response = httpx.get(
            url,
            params=params,
            headers={"User-Agent": settings.user_agent},
            timeout=10,
            follow_redirects=True,
        )
        if response.status_code == 404:
            return NOT_FOUND
        if response.status_code in (429, 503):
            return failed(
                f"Wikipedia HTTP {response.status_code}", retry_after_seconds(response), _throttle
            )
        if response.status_code != 200:
            return failed(f"Wikipedia HTTP {response.status_code}")
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - network or non-JSON reply
        _logger.warning("Wikipedia/Wikidata request failed: %s", urlparse(url).netloc)
        return failed(f"Wikipedia request failed: {type(exc).__name__}")
    return Found(data) if isinstance(data, dict) else failed("Wikipedia returned an invalid response")


def title_from_wikidata(entity_id: str, lang: str = "en") -> ProviderResult[tuple[str, str]]:
    """(language, article title) for a Wikidata entity, English first."""
    result = _get_json(
        "https://www.wikidata.org/w/api.php",
        {"action": "wbgetentities", "ids": entity_id, "props": "sitelinks", "format": "json"},
    )
    if not isinstance(result, Found):
        return result
    entity = (result.data.get("entities") or {}).get(entity_id) or {}
    sitelinks = entity.get("sitelinks") or {}
    for site in (f"{lang}wiki", "enwiki"):
        title = (sitelinks.get(site) or {}).get("title")
        if title:
            return Found((site[:-4], title))
    return NOT_FOUND


def summary(lang: str, title: str) -> ProviderResult[dict]:
    """{"text", "url", "title"} - the article's lead section as plain text."""
    result = _get_json(
        f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}"
    )
    if not isinstance(result, Found):
        return result
    data = result.data
    if not data.get("extract"):
        return NOT_FOUND
    url = ((data.get("content_urls") or {}).get("desktop") or {}).get("page")
    return Found({"text": data["extract"], "url": url, "title": data.get("title") or title})


def summary_from_links(links: list[dict]) -> dict | None:
    """Bio from an artist's MusicBrainz url-rels: a Wikipedia link if there
    is one, else the Wikidata entity's English article. {} when there is
    none; None when a lookup failed and nothing was found (not cacheable)."""
    any_failed = False
    for link in links:
        url = link.get("url") or ""
        parsed = urlparse(url)
        if link.get("type") == "wikipedia" and parsed.netloc.endswith("wikipedia.org"):
            lang = parsed.netloc.split(".")[0]
            title = unquote(parsed.path.rsplit("/", 1)[-1])
            if title:
                found = summary(lang, title)
                if isinstance(found, Found):
                    return found.data
                any_failed |= isinstance(found, Failed)
    for link in links:
        url = link.get("url") or ""
        if link.get("type") == "wikidata" and "/wiki/" in url:
            entity_id = url.rsplit("/", 1)[-1]
            target = title_from_wikidata(entity_id)
            if isinstance(target, Found):
                found = summary(*target.data)
                if isinstance(found, Found):
                    return found.data
                any_failed |= isinstance(found, Failed)
            any_failed |= isinstance(target, Failed)
    return None if any_failed else {}


_TAG_RE = re.compile(r"<[^>]+>")
# Licences whose files may be stored and shown with attribution: CC BY and
# CC BY-SA (any version), CC0, public domain and GFDL. Non-commercial or
# no-derivatives variants, fair use and anything unclear are skipped.
_FREE_LICENCE = re.compile(r"^(cc[ -]by(?:[ -]sa)?(?:[ -]\d|$)|cc0|public domain|pd\b|gfdl)", re.IGNORECASE)


def _https(value: object) -> str | None:
    """An https:// URL, else None - the credit links come from community-
    edited file pages and must not carry any other scheme."""
    text = _plain(value)
    return text if text and text.lower().startswith("https://") else None


def _plain(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(html.unescape(_TAG_RE.sub("", value)).split())
    return text or None


def commons_image(file_name: str, width: int = 1000) -> ProviderResult[dict]:
    """{"url", "thumb", "author", "license", "license_url", "page_url"} for
    a Commons file, scaled to `width`; NotFound when it is missing or not
    under a free licence."""
    result = _get_json(
        "https://commons.wikimedia.org/w/api.php",
        {
            "action": "query",
            "titles": f"File:{file_name}",
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "iiurlwidth": str(width),
            "iiextmetadatafilter": "LicenseShortName|Artist|LicenseUrl",
            "format": "json",
        },
    )
    if not isinstance(result, Found):
        return result
    pages = (result.data.get("query") or {}).get("pages") or {}
    page = next(iter(pages.values()), {}) if isinstance(pages, dict) else {}
    info = (page.get("imageinfo") or [None])[0] if isinstance(page, dict) else None
    if not isinstance(info, dict) or not info.get("url"):
        return NOT_FOUND
    meta = info.get("extmetadata") or {}
    licence = _plain((meta.get("LicenseShortName") or {}).get("value"))
    if not licence or not _FREE_LICENCE.match(licence):
        return NOT_FOUND
    thumb = (info.get("thumburl") or info["url"]).split("?", 1)[0]
    return Found(
        {
            "url": thumb,
            "thumb": thumb,
            "author": _plain((meta.get("Artist") or {}).get("value")),
            "license": licence,
            "license_url": _https((meta.get("LicenseUrl") or {}).get("value")),
            "page_url": _https(info.get("descriptionurl")),
        }
    )


def image_from_wikidata(entity_id: str) -> ProviderResult[dict]:
    """The Commons image (see commons_image) a Wikidata entity names (P18)."""
    result = _get_json(
        "https://www.wikidata.org/w/api.php",
        {"action": "wbgetclaims", "entity": entity_id, "property": "P18", "format": "json"},
    )
    if not isinstance(result, Found):
        return result
    for claim in (result.data.get("claims") or {}).get("P18") or []:
        value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") if isinstance(claim, dict) else None
        if isinstance(value, str) and value:
            return commons_image(value)
    return NOT_FOUND


def image_from_links(links: list[dict]) -> dict | None:
    """An artist photo from its MusicBrainz url-rels' Wikidata entity. {}
    when there is none; None when a lookup failed (not cacheable)."""
    for link in links:
        url = link.get("url") or ""
        if link.get("type") == "wikidata" and "/wiki/" in url:
            found = image_from_wikidata(url.rsplit("/", 1)[-1])
            if isinstance(found, Found):
                return found.data
            if isinstance(found, Failed):
                return None
    return {}
