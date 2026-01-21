import json
import re
import sys
from dataclasses import dataclass
from typing import Iterable, Optional, List, Tuple
from urllib.parse import parse_qs, urlparse

from py_common.config import get_config
from py_common.deps import ensure_requirements
import py_common.log as log
from py_common.util import scraper_args

# Dependencies are installed into scrapers/automatic_dependencies on first run
ensure_requirements("bs4:beautifulsoup4", "python-dateutil", "curl_cffi==0.14.0")

from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from curl_cffi import requests as cffi_requests

DEFAULT_IMPERSONATE = "chrome136"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
PROFILE_URL_TEMPLATE = "https://justfor.fans/{username}"

DEFAULT_CONFIG = """
# Required: your JustForFans numeric user ID
user_id =
# Required: your UserHash4 cookie value
user_hash_4 =
# Optional: poster/performer numeric ID if it can't be inferred from URL
poster_id =
# Optional: performer name to attach to scraped scenes
performer_name =
# Optional: performer profile URL
performer_url =
# Optional: curl_cffi impersonation profile (default chrome136, or use "chrome" for latest)
impersonate = chrome136
# Optional: override user-agent
user_agent =
# Optional: map usernames to poster IDs (format: username:posterid, other=posterid)
poster_id_map =
# Optional: start offset for pagination
start_at = 0
# Optional: max pages to scan when searching for a post
max_pages = 20
# Optional: include locked posts (true/false)
include_locked = false
"""

CONFIG = get_config(DEFAULT_CONFIG)

POSTS_URL = "https://justfor.fans/ajax/getPosts.php"
STUDIO = {"name": "JustForFans", "url": "https://justfor.fans"}


@dataclass
class ParsedPost:
    post_id: str
    post_id_digits: str
    date: Optional[str]
    full_text: str
    text_preview: str
    photos: List[str]
    locked: bool


class ScraperError(RuntimeError):
    pass


def _normalize_digits(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"\D", "", value)


def _extract_ids_from_url(url: Optional[str]) -> Tuple[str, str]:
    if not url:
        return "", ""

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    def qval(*keys: str) -> str:
        for key in keys:
            if key in query and query[key]:
                return str(query[key][0])
        return ""

    post_id = qval("post_id", "postid", "post", "id")
    poster_id = qval("poster_id", "posterid", "creator_id", "userid", "user_id")

    if not post_id:
        path_numbers = re.findall(r"\d+", parsed.path)
        if path_numbers:
            post_id = path_numbers[-1]
            if len(path_numbers) > 1 and not poster_id:
                poster_id = path_numbers[0]

    return post_id, poster_id


def _extract_username_from_url(url: Optional[str]) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return ""
    # Only accept a single path segment as username
    if "/" in path:
        return ""
    return path


def _parse_poster_id_map(value: str) -> dict:
    mapping: dict[str, str] = {}
    if not value:
        return mapping
    for entry in re.split(r"[,\n]", str(value)):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            key, val = entry.split(":", 1)
        elif "=" in entry:
            key, val = entry.split("=", 1)
        else:
            continue
        key = key.strip().lower()
        val = _normalize_digits(val.strip())
        if key and val:
            mapping[key] = val
    return mapping


def _poster_id_from_map(username: str) -> str:
    raw = getattr(CONFIG, "poster_id_map", "") or ""
    mapping = _parse_poster_id_map(raw)
    return mapping.get(username.lower(), "") if username else ""


def _parse_date(raw: str) -> Optional[str]:
    if not raw:
        return None
    cleaned = re.sub(r"This post.*$", "", raw).strip()
    if not cleaned:
        return None
    try:
        return dateparser.parse(cleaned).date().isoformat()
    except Exception:
        return None


def _parse_post(tag) -> ParsedPost:
    post_id = tag.get("id") or tag.get("data-post-id") or ""
    post_id_digits = _normalize_digits(post_id)

    locked = bool(tag.find(class_=lambda c: isinstance(c, str) and "lockedContent" in c))

    text_div = tag.find("div", class_="fr-view")
    full_text = text_div.get_text("\n").strip() if text_div else ""
    text_preview = re.sub(r"\s+", " ", full_text)[:80].strip()

    date_div = tag.find("div", class_="mbsc-card-subtitle")
    date_raw = date_div.get_text(" ").strip() if date_div else ""
    date = _parse_date(date_raw)

    photos: List[str] = []
    for img in tag.find_all("img", class_="expandable"):
        if img.has_attr("data-lazy"):
            photos.append(img["data-lazy"])
        elif img.has_attr("src"):
            photos.append(img["src"])

    return ParsedPost(
        post_id=post_id,
        post_id_digits=post_id_digits,
        date=date,
        full_text=full_text,
        text_preview=text_preview,
        photos=photos,
        locked=locked,
    )


def _is_post_card(tag) -> bool:
    if tag.name != "div":
        return False
    classes = tag.get("class", [])
    if not isinstance(classes, list):
        return False
    if "mbsc-card" not in classes or "jffPostClass" not in classes:
        return False
    if "donotremove" in classes or "shoutout" in classes:
        return False
    if tag.find("div", "storeItemWidget"):
        return False
    return True


def _extract_hashtags(text: str) -> List[str]:
    if not text:
        return []
    tags = {t.strip("#") for t in re.findall(r"#(\w+)", text)}
    return sorted(tags, key=str.lower)


def _impersonate_profile() -> str:
    value = getattr(CONFIG, "impersonate", "")
    return str(value or DEFAULT_IMPERSONATE)


def _user_agent() -> str:
    value = getattr(CONFIG, "user_agent", "")
    return str(value or DEFAULT_USER_AGENT)


def _build_session(user_hash_4: Optional[str]) -> cffi_requests.Session:
    session = cffi_requests.Session()
    session.headers.update({"User-Agent": _user_agent()})
    if user_hash_4:
        session.cookies.set("UserHash4", str(user_hash_4), domain=".justfor.fans", path="/")
    return session


def _request(session: cffi_requests.Session, url: str, **kwargs):
    impersonate = _impersonate_profile()
    if kwargs.get("headers") is None:
        kwargs.pop("headers", None)
    try:
        response = session.get(url, impersonate=impersonate, **kwargs)
    except Exception as exc:
        raise ScraperError(f"Request failed: {exc}")

    if response.status_code >= 400:
        raise ScraperError(f"Request failed with status {response.status_code}")
    lowered = response.text.lower()
    if "just a moment" in lowered and "cloudflare" in lowered:
        raise ScraperError("Request blocked by Cloudflare challenge")
    return response


def _request_posts(
    session, user_id: str, poster_id: str, user_hash_4: str, start_at: int, referer: Optional[str]
) -> str:
    params = {
        "UserID": user_id,
        "PosterID": poster_id,
        "Type": "One",
        "StartAt": str(start_at),
        "Page": "Profile",
        "UserHash4": user_hash_4,
        "SplitTest": "0",
    }
    headers = {"Referer": referer} if referer else None
    response = _request(session, POSTS_URL, params=params, headers=headers)
    return response.text


def _fetch_profile(session, url: str) -> str:
    response = _request(session, url)
    return response.text


def _next_start_at(soup: BeautifulSoup) -> Optional[int]:
    link = soup.find("a", href=lambda href: href and "getPosts.php" in href)
    if not link:
        return None
    match = re.search(r"StartAt=([0-9]+)&", link.get("href", ""))
    if not match:
        return None
    return int(match.group(1))


def _find_target(posts: Iterable[ParsedPost], target_id: str, target_text: str) -> Optional[ParsedPost]:
    target_digits = _normalize_digits(target_id)
    target_text = target_text.lower().strip()

    for post in posts:
        if target_digits and post.post_id_digits == target_digits:
            return post
        if target_text and post.full_text.lower().find(target_text) >= 0:
            return post
    return None


def _meta_content(soup: BeautifulSoup, key: str, value: str) -> str:
    tag = soup.find("meta", attrs={key: value})
    if tag and tag.has_attr("content"):
        return str(tag["content"]).strip()
    return ""


def _clean_profile_name(name: str) -> str:
    if not name:
        return ""
    cleaned = name.strip()
    lowered = cleaned.lower()
    if "justforfans" in lowered or "just for fans" in lowered or "justfor.fans" in lowered:
        for sep in ["|", "•", "-", "—"]:
            if sep in cleaned:
                parts = [p.strip() for p in cleaned.split(sep) if p.strip()]
                if parts:
                    cleaned = parts[0]
                    break
    return cleaned


def _extract_poster_id_from_html(html: str, soup: Optional[BeautifulSoup]) -> str:
    if soup:
        for attr in (
            "data-posterid",
            "data-poster-id",
            "data-poster",
            "data-userid",
            "data-user-id",
        ):
            tag = soup.find(attrs={attr: True})
            if tag:
                value = _normalize_digits(tag.get(attr))
                if value:
                    return value

    patterns = [
        r"PosterID\s*[:=]\s*[\"']?(\d+)",
        r"poster_id\s*[:=]\s*[\"']?(\d+)",
        r"posterId\s*[:=]\s*[\"']?(\d+)",
        r"PosterID=(\d+)",
        r"poster_id=(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)

    # Look for a getPosts.php link containing PosterID
    match = re.search(r"PosterID=(\d+)", html)
    if match:
        return match.group(1)

    return ""


def _extract_performer_from_profile(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    name = _meta_content(soup, "property", "og:title")
    if not name:
        name = _meta_content(soup, "name", "twitter:title")
    if not name:
        title_tag = soup.find("title")
        name = title_tag.get_text(strip=True) if title_tag else ""
    name = _clean_profile_name(name)

    if not name:
        slug = _extract_username_from_url(url)
        name = slug.replace("_", " ") if slug else "JustForFans performer"

    description = _meta_content(soup, "property", "og:description")
    if not description:
        description = _meta_content(soup, "name", "twitter:description")

    image = _meta_content(soup, "property", "og:image")
    if not image:
        image = _meta_content(soup, "name", "twitter:image")

    performer = {"name": name, "urls": [url]}
    if description:
        performer["details"] = description
    if image:
        performer["images"] = [image]

    return performer


def _select_scene_performer(parsed_performer: Optional[dict], fallback_url: Optional[str]) -> Optional[dict]:
    performer_name = getattr(CONFIG, "performer_name", "")
    performer_url = getattr(CONFIG, "performer_url", "") or fallback_url

    if performer_name:
        performer = {"name": performer_name}
        if performer_url:
            performer["url"] = performer_url
        return performer

    if parsed_performer:
        performer = {"name": parsed_performer.get("name", "JustForFans performer")}
        urls = parsed_performer.get("urls") or []
        if urls:
            performer["url"] = urls[0]
        elif fallback_url:
            performer["url"] = fallback_url
        return performer

    return None


def _build_scene(post: ParsedPost, url: Optional[str], performer: Optional[dict]) -> dict:
    title = post.text_preview or "JustForFans post"
    if post.date:
        title = f"{title} ({post.date})"

    scene = {
        "title": title,
        "details": post.full_text or None,
        "date": post.date,
        "url": url,
        "urls": [url] if url else None,
        "code": post.post_id_digits or post.post_id or None,
        "studio": STUDIO,
    }

    if post.photos:
        scene["image"] = post.photos[0]

    tags = _extract_hashtags(post.full_text)
    if tags:
        scene["tags"] = [{"name": tag} for tag in tags]

    if performer:
        scene["performers"] = [performer]

    return {k: v for k, v in scene.items() if v not in (None, [], "")}


def _int_config(name: str, default: int) -> int:
    value = getattr(CONFIG, name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_profile(session, url: Optional[str]) -> Tuple[str, Optional[dict]]:
    if not url:
        return "", None

    html = _fetch_profile(session, url)
    performer = _extract_performer_from_profile(url, html)
    poster_id = _extract_poster_id_from_html(html, None)
    if not poster_id:
        soup = BeautifulSoup(html, "html.parser")
        poster_id = _extract_poster_id_from_html(html, soup)
    return poster_id, performer




def _scrape_scene(url: Optional[str], target_id: str, target_text: str) -> dict:
    user_id = getattr(CONFIG, "user_id", None)
    user_hash_4 = getattr(CONFIG, "user_hash_4", None)
    poster_id = getattr(CONFIG, "poster_id", None)

    if not user_id or not user_hash_4:
        raise ScraperError("Missing required config: user_id and user_hash_4")

    url_post_id, url_poster_id = _extract_ids_from_url(url)
    if not target_id:
        target_id = url_post_id
    if not poster_id:
        poster_id = url_poster_id

    profile_url = url or getattr(CONFIG, "performer_url", "")

    session = _build_session(user_hash_4)

    performer = None
    username = _extract_username_from_url(profile_url)
    if not poster_id and username:
        poster_id = _poster_id_from_map(username)
    if profile_url and (not poster_id or not getattr(CONFIG, "performer_name", "")):
        try:
            resolved_poster_id, resolved_performer = _resolve_profile(session, profile_url)
            if not poster_id and resolved_poster_id:
                poster_id = resolved_poster_id
            performer = resolved_performer
        except ScraperError as exc:
            log.warning(f"Profile fetch failed: {exc}")

    if not poster_id:
        if username:
            log.warning(
                "Poster ID not found. Set poster_id_map in config.ini, e.g. "
                f"poster_id_map = {username}:{'<poster_id>'}"
            )
        else:
            log.warning(
                "Poster ID not found. Set poster_id in config.ini or "
                "poster_id_map = username:poster_id"
            )
        raise ScraperError("Missing poster_id (set in config or include in URL)")

    start_at = _int_config("start_at", 0)
    max_pages = _int_config("max_pages", 20)
    include_locked = bool(getattr(CONFIG, "include_locked", False))

    current = start_at
    pages = 0
    latest_post: Optional[ParsedPost] = None

    while pages < max_pages:
        pages += 1
        log.debug(f"Fetching posts: start_at={current}")
        html = _request_posts(
            session,
            str(user_id),
            str(poster_id),
            str(user_hash_4),
            current,
            profile_url or None,
        )
        soup = BeautifulSoup(html, "html.parser")

        cards: List[ParsedPost] = []
        for tag in soup.find_all(_is_post_card):
            post = _parse_post(tag)
            if post.locked and not include_locked:
                continue
            cards.append(post)

        if cards and latest_post is None:
            latest_post = cards[0]

        if target_id or target_text:
            match = _find_target(cards, target_id, target_text)
            if match:
                performer_payload = _select_scene_performer(performer, profile_url)
                return _build_scene(match, url, performer_payload)
        else:
            if latest_post:
                performer_payload = _select_scene_performer(performer, profile_url)
                return _build_scene(latest_post, url, performer_payload)

        next_start = _next_start_at(soup)
        if next_start is None or next_start <= current:
            break
        current = next_start

    if target_id or target_text:
        raise ScraperError("Target post not found in scanned pages")

    if latest_post:
        performer_payload = _select_scene_performer(performer, profile_url)
        return _build_scene(latest_post, url, performer_payload)

    raise ScraperError("No posts available for this performer")


def _scrape_performer(url: Optional[str], name: Optional[str]) -> dict:
    user_hash_4 = getattr(CONFIG, "user_hash_4", None)
    if not user_hash_4:
        raise ScraperError("Missing required config: user_hash_4")

    if not url and name:
        slug = re.sub(r"\s+", "_", name.strip())
        url = PROFILE_URL_TEMPLATE.format(username=slug)

    if not url:
        raise ScraperError("Missing performer URL")

    session = _build_session(user_hash_4)
    html = _fetch_profile(session, url)
    performer = _extract_performer_from_profile(url, html)
    return performer


def _performer_search(name: str) -> List[dict]:
    slug = re.sub(r"\s+", "_", name.strip())
    url = PROFILE_URL_TEMPLATE.format(username=slug)
    return [{"name": name, "url": url}]


def main():
    op, args = scraper_args()
    url = args.get("url") or (args.get("urls") or [None])[0]
    target_id = str(args.get("id") or args.get("code") or "")
    target_text = str(args.get("title") or args.get("details") or "")

    if op in ("scene-by-url", "scene-by-fragment", "scene-by-query-fragment"):
        result = _scrape_scene(url, target_id, target_text)
    elif op in ("performer-by-url", "performer-by-fragment"):
        result = _scrape_performer(url, args.get("name"))
    elif op == "performer-by-name":
        name = args.get("name")
        if not name:
            raise ScraperError("Missing performer name")
        result = _performer_search(name)
    else:
        raise ScraperError(f"Unsupported operation: {op}")

    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except ScraperError as exc:
        log.error(exc)
        print("null")
        sys.exit(1)
