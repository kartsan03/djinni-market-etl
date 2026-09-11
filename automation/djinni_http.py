import cloudscraper

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}
BLOCK_MARKERS = ("has been blocked", "cf-chl-")


class BlockedResponseError(RuntimeError):
    pass


def is_blocked_response(text):
    lowered = (text or "").lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


def raise_if_blocked(text, message="Djinni/Cloudflare block page detected"):
    if is_blocked_response(text):
        raise BlockedResponseError(message)


def create_djinni_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
