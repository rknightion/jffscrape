import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Iterable, Optional, List, Tuple
from urllib.parse import parse_qs, urlparse

SCRAPERS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if SCRAPERS_ROOT not in sys.path:
    sys.path.insert(0, SCRAPERS_ROOT)

try:
    from py_common.config import get_config
    from py_common.deps import ensure_requirements
    import py_common.log as log
    from py_common.util import scraper_args
except ModuleNotFoundError as exc:
    if exc.name and exc.name.startswith("py_common"):
        sys.stderr.write(
            "Missing dependency package 'py_common'. Ensure your scraper source "
            "installs dependencies and that this directory exists:\n"
            f"  {os.path.join(SCRAPERS_ROOT, 'py_common')}\n"
            "If you installed manually, copy scrapers/py_common alongside JustForFans.\n"
        )
        print("null")
        sys.exit(1)
    raise

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
    post_type: str
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


def _normalize_key(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _normalize_text(value: str) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"[_\-]+", " ", value)
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _extract_date_from_string(value: str) -> Optional[str]:
    if not value:
        return None
    match = re.search(r"(20\d{2})[^\d]?(\d{2})[^\d]?(\d{2})", value)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def _normalize_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return dateparser.parse(str(value)).date().isoformat()
    except Exception:
        return None


def _strip_datetime_prefix(value: str) -> str:
    if not value:
        return ""
    value = _normalize_text(value)
    value = re.sub(r"^20\d{2}\s\d{2}\s\d{2}(\s\d{2}\s\d{2}\s\d{2})?\s*", "", value)
    return value.strip()


def _extract_keywords(value: str) -> List[str]:
    value = _strip_datetime_prefix(value)
    if not value:
        return []
    stopwords = {
        "the",
        "and",
        "with",
        "for",
        "from",
        "this",
        "that",
        "you",
        "your",
        "his",
        "her",
        "their",
        "its",
        "into",
        "onto",
        "over",
        "under",
        "video",
        "scene",
        "clip",
        "part",
        "episode",
    }
    words = []
    for word in value.split():
        if len(word) < 3 or word.isdigit() or word in stopwords:
            continue
        words.append(word)
    # Preserve order, remove duplicates
    seen = set()
    ordered = []
    for word in words:
        if word not in seen:
            seen.add(word)
            ordered.append(word)
    return ordered


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
        key = _normalize_key(key.strip())
        val = _normalize_digits(val.strip())
        if key and val:
            mapping[key] = val
    return mapping


def _poster_id_from_map(username: str) -> str:
    raw = getattr(CONFIG, "poster_id_map", "") or ""
    mapping = _parse_poster_id_map(raw)
    normalized = _normalize_key(username) if username else ""
    return mapping.get(normalized, "") if normalized else ""


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

    classes = tag.get("class", [])
    post_type = "unknown"
    if isinstance(classes, list):
        if "video" in classes:
            post_type = "video"
        elif "photo" in classes:
            post_type = "photo"
        elif "text" in classes:
            post_type = "text"

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
        post_type=post_type,
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


def _is_gallery_candidate(post: ParsedPost) -> bool:
    if post.post_type == "photo":
        return True
    if post.photos and post.post_type != "video":
        return True
    return False


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


def _find_target(
    posts: Iterable[ParsedPost],
    target_id: str,
    target_text: str,
    target_date: Optional[str],
    target_keywords: List[str],
) -> Optional[ParsedPost]:
    target_digits = _normalize_digits(target_id)
    target_text_norm = _normalize_text(target_text)

    if target_digits:
        for post in posts:
            if post.post_id_digits == target_digits:
                return post

    posts_list = list(posts)
    if not posts_list:
        return None

    if target_date:
        date_posts = [post for post in posts_list if post.date == target_date]
        if not date_posts:
            return None
        if not target_keywords:
            return date_posts[0]
        best = None
        best_score = -1
        for post in date_posts:
            haystack = _normalize_text(post.full_text or post.text_preview)
            score = sum(1 for kw in target_keywords if kw in haystack)
            if score > best_score:
                best_score = score
                best = post
        return best if best else date_posts[0]

    if target_text_norm:
        for post in posts_list:
            haystack = _normalize_text(post.full_text or post.text_preview)
            if target_text_norm and target_text_norm in haystack:
                return post

    if target_keywords:
        best = None
        best_score = -1
        for post in posts_list:
            haystack = _normalize_text(post.full_text or post.text_preview)
            score = sum(1 for kw in target_keywords if kw in haystack)
            if score > best_score:
                best_score = score
                best = post
        return best if best_score > 0 else None

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

def _normalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    return url


def _extract_social_links(soup: BeautifulSoup) -> dict:
    twitter = ""
    instagram = ""
    bluesky = ""
    extra_urls: List[str] = []

    for tag in soup.find_all("a", href=True):
        href = _normalize_url(str(tag.get("href", "")).strip())
        if not href or href.startswith("javascript:"):
            continue
        lower = href.lower()
        if "twitter.com/" in lower or "x.com/" in lower:
            if not twitter:
                twitter = href
            continue
        if "instagram.com/" in lower:
            if not instagram:
                instagram = href
            continue
        if "bsky.app/" in lower or "bsky.social" in lower:
            if not bluesky:
                bluesky = href
            else:
                extra_urls.append(href)

    return {
        "twitter": twitter,
        "instagram": instagram,
        "bluesky": bluesky,
        "extra_urls": extra_urls,
    }


def _looks_generic_description(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    generic_phrases = [
        "justfor.fans",
        "just for fans",
        "login for free",
        "interact with your favorite",
        "text them, chat with them",
        "watch their videos",
    ]
    return all(phrase in lowered for phrase in generic_phrases[:3]) or any(
        phrase in lowered for phrase in generic_phrases[3:]
    )


def _extract_profile_bio(soup: BeautifulSoup) -> str:
    # Prefer explicit profile text blocks if present
    for block_id in ("profileTextLarge", "profileTextSmall"):
        block = soup.find(id=block_id)
        if not block:
            continue
        p = block.find("p")
        text = p.get_text(" ", strip=True) if p else block.get_text(" ", strip=True)
        text = text.replace("Read More", "").strip()
        if text and len(text) >= 10:
            return text

    candidates: List[str] = []
    patterns = re.compile(r"(bio|about|description|blurb|profile)", re.I)

    for tag in soup.find_all(True, class_=patterns):
        text = tag.get_text(" ", strip=True)
        if text and len(text) >= 10:
            candidates.append(text)

    for tag in soup.find_all(True, id=patterns):
        text = tag.get_text(" ", strip=True)
        if text and len(text) >= 10:
            candidates.append(text)

    # Prefer the longest candidate as a simple heuristic
    if candidates:
        candidates.sort(key=len, reverse=True)
        return candidates[0]
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
    if description and _looks_generic_description(description):
        description = ""
    if not description:
        description = _extract_profile_bio(soup)

    image = _meta_content(soup, "property", "og:image")
    if not image:
        image = _meta_content(soup, "name", "twitter:image")

    urls = [url]
    socials = _extract_social_links(soup)
    if socials.get("bluesky"):
        urls.append(socials["bluesky"])
    if socials.get("extra_urls"):
        urls.extend(socials["extra_urls"])

    performer = {"name": name, "urls": list(dict.fromkeys([u for u in urls if u]))}
    if description:
        performer["details"] = description
    if image:
        performer["images"] = [image]
    if socials.get("twitter"):
        performer["twitter"] = socials["twitter"]
    if socials.get("instagram"):
        performer["instagram"] = socials["instagram"]

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


def _build_gallery(post: ParsedPost, url: Optional[str], performer: Optional[dict]) -> dict:
    title = post.text_preview or "JustForFans post"
    if post.date:
        title = f"{title} ({post.date})"

    gallery = {
        "title": title,
        "details": post.full_text or None,
        "date": post.date,
        "url": url,
        "urls": post.photos or None,
        "code": post.post_id_digits or post.post_id or None,
        "studio": STUDIO,
    }

    tags = _extract_hashtags(post.full_text)
    if tags:
        gallery["tags"] = [{"name": tag} for tag in tags]

    if performer:
        gallery["performers"] = [performer]

    return {k: v for k, v in gallery.items() if v not in (None, [], "")}


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




def _scrape_post(
    url: Optional[str],
    target_id: str,
    target_text: str,
    target_date: Optional[str],
    target_keywords: List[str],
    require_gallery: bool,
) -> Tuple[ParsedPost, Optional[dict], Optional[str]]:
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

    target_label = target_id or (target_date or ("text" if target_text else "latest"))
    log.info(
        f"Scraping {'gallery' if require_gallery else 'scene'} for user "
        f"{username or 'n/a'} (poster_id={poster_id}, target={target_label})"
    )

    start_at = _int_config("start_at", 0)
    max_pages = _int_config("max_pages", 20)
    include_locked = bool(getattr(CONFIG, "include_locked", False))

    log.info(
        f"Options: start_at={start_at}, max_pages={max_pages}, include_locked={include_locked}"
    )

    current = start_at
    pages = 0
    latest_post: Optional[ParsedPost] = None

    def post_ok(post: ParsedPost) -> bool:
        if require_gallery:
            return _is_gallery_candidate(post)
        return True

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
            if not post_ok(post):
                continue
            cards.append(post)
            if latest_post is None:
                latest_post = post

        if target_id or target_text or target_date or target_keywords:
            match = _find_target(cards, target_id, target_text, target_date, target_keywords)
            if match:
                performer_payload = _select_scene_performer(performer, profile_url)
                return match, performer_payload, profile_url
        else:
            if latest_post:
                performer_payload = _select_scene_performer(performer, profile_url)
                return latest_post, performer_payload, profile_url

        next_start = _next_start_at(soup)
        if next_start is None or next_start <= current:
            break
        current = next_start

    if target_id or target_text or target_date or target_keywords:
        raise ScraperError("Target post not found in scanned pages")

    if latest_post:
        performer_payload = _select_scene_performer(performer, profile_url)
        return latest_post, performer_payload, profile_url

    raise ScraperError("No posts available for this performer")


def _scrape_scene(
    url: Optional[str],
    target_id: str,
    target_text: str,
    target_date: Optional[str],
    target_keywords: List[str],
) -> dict:
    post, performer, profile_url = _scrape_post(
        url, target_id, target_text, target_date, target_keywords, False
    )
    return _build_scene(post, url or profile_url, performer)


def _scrape_gallery(
    url: Optional[str],
    target_id: str,
    target_text: str,
    target_date: Optional[str],
    target_keywords: List[str],
) -> dict:
    post, performer, profile_url = _scrape_post(
        url, target_id, target_text, target_date, target_keywords, True
    )
    if not post.photos:
        raise ScraperError("Selected post does not contain photos")
    return _build_gallery(post, url or profile_url, performer)


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
    raw_title = str(args.get("title") or "")
    raw_details = str(args.get("details") or "")
    target_text = raw_title or raw_details
    target_date = _normalize_date(args.get("date")) or _extract_date_from_string(raw_title)
    target_keywords = _extract_keywords(f"{raw_title} {raw_details}".strip())

    if op in ("scene-by-url", "scene-by-fragment", "scene-by-query-fragment"):
        result = _scrape_scene(url, target_id, target_text, target_date, target_keywords)
    elif op in ("gallery-by-url", "gallery-by-fragment"):
        result = _scrape_gallery(url, target_id, target_text, target_date, target_keywords)
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
