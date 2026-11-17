"""Bot detection for email open / click tracking.

Identifies non-human traffic from:

- Mailbox clients that prefetch or scan tracking URLs (Outlook desktop, Outlook
  for Web, Apple Mail Privacy Protection, Gmail image proxy, link-preview bots)
- Anti-spam / anti-phishing scanners (Microsoft Defender ATP / SafeLinks,
  Mimecast, Proofpoint URL Defense, Barracuda)
- Headless browsers and HTTP libraries (Selenium, curl, python-requests,
  Go-http-client, Ruby, Java)
- Forged-UA scrapers running on cloud VPS (AWS, GCP, Azure, DigitalOcean)
- VPNs/CDNs that proxy a single client across many IPs (Cloudflare WARP,
  Fastly)

Returns ``(is_bot, reason)`` so callers can both filter the event out of
metrics AND record an audit trail with the specific rule that fired.

Besides the generic crawler and library rules, the email flavor covers
Outlook/Trident/MSOffice templates, Apple MPP by IP, and broad cloud/CDN
filtering.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Pattern

# UA patterns: (compiled regex, reason tag). Order matters — first match wins.
_UA_PATTERNS: list[tuple[Pattern[str], str]] = []


def _ua(pattern: str, reason: str) -> None:
    _UA_PATTERNS.append((re.compile(pattern, re.IGNORECASE), reason))


# 1. Mail-client prefetchers (HIGH confidence — these are not user reads)
_ua(r"GoogleImageProxy", "mail-client:gmail-image-proxy")
_ua(r"YahooMailProxy", "mail-client:yahoo-mail-proxy")
_ua(r"\bms-office\b|\bMSOffice\s*(\d{1,2}|rmj)\b",
    "mail-client:outlook-desktop-prefetch")
_ua(r"Microsoft Office (Outlook|Word|Excel)", "mail-client:office-suite")

# 2. Anti-phishing / URL scanners (security gateways)
_ua(r"\b(safelinks|defender|atp[- ]scanner|urldefense|proofpoint|mimecast|"
    r"barracuda|sophos|cisco-ironport|forcepoint|trendmicro|symantec)\b",
    "security-scanner:url-defense")
_ua(r"\b(facebookexternalhit|facebot|meta-external(agent|fetcher)?|whatsapp|"
    r"telegrambot|slackbot(?:-linkexpanding)?|discordbot|linkedinbot|twitterbot|"
    r"pinterest(bot)?|skypeuripreview|redditbot|chatgpt|gptbot|claudebot|"
    r"perplexity(bot)?)\b",
    "crawler:link-preview")

# 3. Self-declared crawlers / search engines
_ua(r"\b(googlebot|adsbot-google|google-inspectiontool|bingbot|msnbot|"
    r"yandex(bot|images)?|duckduckbot|applebot|baiduspider|sogou|exabot|"
    r"semrushbot|ahrefsbot|mj12bot|dotbot|petalbot)\b",
    "crawler:search-engine")
_ua(r"\b(crawler|spider|slurp|fetcher|monitor|validator|preview|unfurl|"
    r"archiver|indexer|checker|scraper)\b",
    "crawler:generic-keyword")
_ua(r"(?<![a-z])bot(?![a-z])", "crawler:bot-keyword")
_ua(r"^antispam[/ ]", "self-declared:antispam")

# 4. HTTP libraries / scripted clients (Ruby/Java/Python/etc. without a fronting browser)
_ua(r"^(curl|wget|httpie|libwww-perl|lwp-trivial|python-requests|python-urllib|"
    r"aiohttp|axios|node-fetch|got/|undici|guzzlehttp|http_request|java/|"
    r"apache-httpclient|okhttp|go-http-client|reqwest|hyper/|restsharp|"
    r"http\.rb|faraday|typhoeus|mechanize|ruby)",
    "http-library")
_ua(r"\b(headlesschrome|headless|phantomjs|selenium|puppeteer|playwright|"
    r"chromedriver|webdriver|nightmare|cypress|katalon|splash|appengine-google)\b",
    "headless-browser")

# 5. Truncated / template UAs (legitimate clients always send a fuller string)
_ua(r"^Mozilla/[45]\.0\s*$", "truncated-ua:mozilla-bare")

# 6. Outlook desktop / Trident render engines (almost always prefetch, not read)
# MSIE 7/10 inside Trident is the embedded IE rendering Outlook uses to display
# HTML — when it appears as a UA on a *tracking* request it's the message scan
# pass, not the user opening the message.
_ua(r"\bMSIE\s+(7|8|9|10)\.0\b.*Trident/", "mail-client:outlook-trident-render")
_ua(r"\bTrident/7\.0\b", "mail-client:trident-7-render")

# 7. Forged UA templates (seen in tracking traffic)
# Do NOT add recent Android builds here: Build/BP2A|BP4A are REAL Android 16
# builds, and a rule like that would flag real users.
_ua(r"Build/NRD90M", "forged:android-7-template-NRD90M")
_ua(r"SCH-I535|Build/KOT49H", "forged:galaxy-s3-kitkat-template")
_ua(r"CriOS/56\.0\.2924\.75", "forged:crios-56-template")

# 8. Versions that don't exist in the wild yet (UA spoofing).
# Calibrate the thresholds against the REAL stable versions (chromereleases /
# whattrainisitnow), never from memory: a threshold at or below the current
# stable release would classify real users as bots. As of 2026-07: Chrome
# stable 150 (151 beta), CriOS ~150, Firefox 153, Safari (iOS 26) Version ~26.
# Buffer of ~10 versions (~1 year of headroom for engines on a 4-week cycle).
_ua(r"\bChrome/(1[6-9]\d|[2-9]\d{2})\.",
    "forged:chrome-future-version")
_ua(r"\bCriOS/(1[6-9]\d|[2-9]\d{2})\.",
    "forged:crios-future-version")
_ua(r"\bFirefox/(1[6-9]\d|[2-9]\d{2})\.",
    "forged:firefox-future-version")
_ua(r"Version/(3[5-9]|[4-9]\d)\.\d+\s+(Mobile|Safari)",
    "forged:safari-future-version")

# 9. End-of-life desktop / mobile platforms — in 2026 these are virtually all bots
_ua(r"Windows NT 6\.[0-3]\b", "eol:windows-7-or-8")
_ua(r"Windows NT 5\.\d", "eol:windows-xp-or-2000")
_ua(r"Android [4-7]\.", "eol:android-pre-8")

# 10. EOL Firefox versions seen as Selenium templates
_ua(r"Firefox/3[3-9]\.0\b", "eol:firefox-pre-40")
_ua(r"Win64; x64; rv:109\.0.*Firefox/110\.0",
    "forged:firefox-110-selenium-template")
_ua(r"rv:109\.0.*Firefox/109\.0", "forged:firefox-109-selenium-template")


# Bot IP networks (CIDR). All IPv4. Apple MPP is detected by IP, not UA.
_BOT_NETWORKS_V4: list[tuple[ipaddress.IPv4Network, str]] = []


def _ip(cidr: str, reason: str) -> None:
    _BOT_NETWORKS_V4.append((ipaddress.ip_network(cidr), reason))


# Apple Mail Privacy Protection / iCloud Private Relay (per Apple's published list).
# Apple owns the entire 17.0.0.0/8.
_ip("17.0.0.0/8", "apple:mpp")

# Google URL preview / Image Proxy / Safe Browsing
_ip("66.249.64.0/19", "google:url-preview")
_ip("66.249.80.0/20", "google:url-preview")
_ip("66.102.0.0/20", "google:proxy")
_ip("64.233.160.0/19", "google:proxy")
_ip("72.14.192.0/18", "google:proxy")
_ip("74.125.0.0/16", "google:proxy")
_ip("173.194.0.0/16", "google:proxy")
_ip("209.85.128.0/17", "google:proxy")
_ip("35.190.247.0/24", "google:proxy")
_ip("35.191.0.0/16", "google:proxy")

# Microsoft 365 SafeLinks / Defender ATP scanners (subset of Azure)
_ip("40.92.0.0/15", "microsoft:safelinks")
_ip("40.107.0.0/16", "microsoft:safelinks")
_ip("40.94.0.0/16", "microsoft:safelinks")
_ip("52.96.0.0/14", "microsoft:safelinks")
_ip("52.100.0.0/14", "microsoft:safelinks")
_ip("104.47.0.0/17", "microsoft:safelinks")

# Cloud VPS providers — used by anti-phishing scanners (Mimecast, Proofpoint,
# Barracuda, custom). Legitimate humans clicking email don't browse from cloud.
# AWS major prefixes (subset)
_ip("3.0.0.0/8", "aws:scanner")
_ip("13.0.0.0/8", "aws:scanner")
_ip("18.0.0.0/8", "aws:scanner")
_ip("52.0.0.0/8", "aws:scanner")
_ip("54.0.0.0/8", "aws:scanner")
# GCP
_ip("34.0.0.0/8", "gcp:scanner")
_ip("35.0.0.0/8", "gcp:scanner")
_ip("104.196.0.0/14", "gcp:scanner")
_ip("104.154.0.0/15", "gcp:scanner")
# Azure (broad, intentionally; SafeLinks above takes precedence by order)
_ip("20.0.0.0/8", "azure:scanner")
_ip("40.64.0.0/10", "azure:scanner")
# DigitalOcean
_ip("64.225.0.0/16", "digitalocean:scanner")
_ip("157.230.0.0/16", "digitalocean:scanner")
_ip("161.35.0.0/16", "digitalocean:scanner")
_ip("167.99.0.0/16", "digitalocean:scanner")
_ip("178.62.0.0/16", "digitalocean:scanner")
# HostRoyale BR (seen in scanner traffic)
_ip("172.111.15.0/24", "hostroyale:scanner")
# Cloudflare WARP egress (consumer VPN)
_ip("104.28.0.0/16", "cloudflare:warp")
_ip("162.158.0.0/15", "cloudflare:warp")
_ip("172.69.0.0/16", "cloudflare:warp")
_ip("172.70.0.0/16", "cloudflare:warp")
_ip("108.162.192.0/18", "cloudflare:warp")
# Fastly CDN (proxy / scanner forwarding)
_ip("146.75.0.0/16", "fastly:cdn")
_ip("151.101.0.0/16", "fastly:cdn")
_ip("199.232.0.0/16", "fastly:cdn")


MAX_REASON_LEN = 64

# For OPENS, the Gmail/Yahoo image proxy is NOT a bot: every real open by a
# Gmail user arrives through GoogleImageProxy (UA + Google egress), so treating
# it as a bot would drop practically every Gmail open. These proxies' prefetch
# at delivery time is filtered by the minimum delivered->open delta gate, not
# here. For CLICKS these UAs/IPs are still bots (a click from Google
# infrastructure is link scanning, not a user).
# Note: the proxy match must SHORT-CIRCUIT (not continue to the other rules),
# because the GoogleImageProxy UA embeds old strings such as "Windows NT 5.1"
# that would trigger the EOL platform rule.
_OPEN_EXEMPT_REASONS = frozenset({
    "mail-client:gmail-image-proxy",
    "mail-client:yahoo-mail-proxy",
    "ip:google:proxy",
    "ip:google:url-preview",
})


def _truncate(reason: str) -> str:
    return reason[:MAX_REASON_LEN]


def detect_bot(
    user_agent: str | None,
    ip_address: str | None = None,
    event_type: str = "click",
) -> tuple[bool, str | None]:
    """Classify an email tracking event (open or click) as bot or human.

    Args:
        user_agent: Raw User-Agent header from the request.
        ip_address: Client IP after X-Forwarded-For resolution.
        event_type: ``"open"`` or ``"click"``. Opens exempt the Gmail/Yahoo
            image-proxy signatures (see ``_OPEN_EXEMPT_REASONS``).

    Returns:
        ``(is_bot, reason)``. ``reason`` is a stable short tag like
        ``"mail-client:outlook-desktop-prefetch"`` or ``"ip:apple:mpp"`` and is
        ``None`` when ``is_bot`` is False.
    """
    is_open = event_type == "open"

    if not user_agent or not user_agent.strip():
        return True, "empty-user-agent"

    for regex, reason in _UA_PATTERNS:
        if regex.search(user_agent):
            if is_open and reason in _OPEN_EXEMPT_REASONS:
                return False, None
            return True, _truncate(reason)

    if ip_address:
        try:
            ip = ipaddress.ip_address(ip_address)
        except ValueError:
            ip = None
        if isinstance(ip, ipaddress.IPv4Address):
            for net, reason in _BOT_NETWORKS_V4:
                if ip in net:
                    if is_open and f"ip:{reason}" in _OPEN_EXEMPT_REASONS:
                        return False, None
                    return True, _truncate(f"ip:{reason}")

    return False, None
