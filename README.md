# JustForFans Stash Scraper

Script-based Stash scrapers for JustForFans performer pages. It uses `curl_cffi` impersonation to get past Cloudflare and returns metadata for a single post at a time (latest post by default).

## What this scraper does

- Scrape a performer profile URL (e.g. `https://justfor.fans/BoundEagle1`).
- Fetch post cards from JFF's `getPosts.php` endpoint.
- Return a single Stash scene (latest visible post or a matching post when an ID/keyword is provided).
- Scrape performer metadata (name, bio, image) from the profile page.

## Install

1. Copy this repo (or just the `scrapers` folder) into your Stash scrapers path.
2. In Stash, re-scan scrapers if needed.
3. Run any scrape once to generate `scrapers/JustForFans/config.ini`.

## Required config

Edit `scrapers/JustForFans/config.ini`:

```ini
user_id = 123456
user_hash_4 = your_userhash4_cookie_value
```

### How to find `user_id` and `user_hash_4`

- Log into JustForFans in your browser.
- Open DevTools -> Application (or Storage) -> Cookies -> `justfor.fans`.
- Copy the value for the `UserHash4` cookie.
- Find your numeric `user_id` from the same session (it appears in the `getPosts.php` request as `UserID=...`).

## Poster ID mapping (important)

JustForFans does not expose `PosterID` in the performer URL or in the profile HTML. You must map usernames to their numeric poster IDs.

Add this to `scrapers/JustForFans/config.ini`:

```ini
poster_id_map = BearwoodBruiser:17233, BoundEagle1:1313658, blake_m_davies:2101812
```

### How to find a performer's `PosterID`

1. Open the performer profile in your browser.
2. Open DevTools -> Network.
3. Scroll the page so posts load.
4. Find the request to `https://justfor.fans/ajax/getPosts.php`.
5. Copy the `PosterID=...` value from the request URL.

## Optional config

```ini
# Attach performer info to scenes (if you want to override profile parsing)
performer_name =
performer_url =

# curl_cffi browser impersonation (default chrome136). Set to `chrome` for newest.
impersonate = chrome136
# Optional UA override
user_agent =

# Pagination controls
start_at = 0
max_pages = 20

# Include locked preview posts
include_locked = false
```

## Usage in Stash

- **Scene scraping**: Use “Scrape with…” on a performer URL such as:
  - `https://justfor.fans/BoundEagle1`
  - `https://justfor.fans/blake_m_davies`
- If no post ID is supplied, the scraper returns the latest visible post.
- If Stash passes an ID or title fragment, the scraper will scan pages to find a matching post.

- **Performer scraping**: Use “Scrape with…” on the performer URL to populate the performer entry.

## Files

- `scrapers/JustForFans/JustForFans.yml` - Stash scraper definition
- `scrapers/JustForFans/JustForFans.py` - Script scraper implementation
- `scrapers/JustForFans/config.example.ini` - Example configuration template
- `scrapers/py_common` - Helper utilities used by script scrapers

## Troubleshooting

- **Cloudflare block**: Try `impersonate = chrome` or a higher chrome profile value, and refresh `user_hash_4`.
- **Missing poster ID**: Add `poster_id_map` for the username. The scraper will log a hint when this is missing.
- **No posts found**: Increase `max_pages` and confirm your subscription/access to the performer.
