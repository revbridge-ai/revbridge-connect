"""Tests for the email tracking bot detection rules."""
from mail_tracking.services.bot_detection import detect_bot

# UA samples of real-world mail clients and scanners
OUTLOOK_MSOFFICE_16 = "Mozilla/4.0 (compatible; ms-office; MSOffice 16)"
OUTLOOK_TRIDENT = (
    "Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 10.0; WOW64; Trident/7.0; "
    ".NET4.0C; .NET4.0E; .NET CLR 2.0.50727; .NET CLR 3.0.30729; .NET CLR 3.5.30729)"
)
OUTLOOK_MSIE10 = (
    "Mozilla/5.0 (compatible; MSIE 10.0; Windows NT 10.0; WOW64; Trident/7.0; "
    "Microsoft Outlook 16.0)"
)
MSOFFICE_RMJ = "Mozilla/4.0 (compatible; ms-office; MSOffice rmj)"
MOZILLA_TRUNCATED_5 = "Mozilla/5.0"
MOZILLA_TRUNCATED_4 = "Mozilla/4.0"
GMAIL_PROXY = "Mozilla/5.0 (Windows; U; Windows NT 5.1; en-US; rv:1.7.12) GoogleImageProxy"
CHROME_FUTURE_165 = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/165.0.0.0 Mobile Safari/537.36"
)
WIN81_FIREFOX110 = (
    "Mozilla/5.0 (Windows NT 6.3; Win64; x64; rv:109.0) Gecko/20100101 Firefox/110.0"
)
EOL_FIREFOX_33 = "Mozilla/5.0 (Windows NT 5.1; rv:33.0) Gecko/20100101 Firefox/33.0"
GO_HTTP_CLIENT = "Go-http-client/2.0"
PYTHON_REQUESTS = "python-requests/2.32.5"
RUBY_LIB = "Ruby"

# Real humans
REAL_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
REAL_GMAIL_WEB_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
REAL_THUNDERBIRD = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:115.0) Gecko/20100101 Thunderbird/115.0"
)
# Current real-world versions (2026-07): Chrome stable 150, Firefox 153,
# Safari/iOS 26, Android 16 Build/BP2A. The "future-version" thresholds must never
# flag these.
REAL_CHROME_150 = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
REAL_CRIOS_150 = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) CriOS/150.0.7871.128 Mobile/15E148 Safari/604.1"
)
REAL_FIREFOX_153 = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0"
)
REAL_SAFARI_IOS26 = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1"
)
REAL_ANDROID16_BP2A = (
    "Mozilla/5.0 (Linux; Android 16; SM-S711B Build/BP2A.250605.031.A3; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/137.0.0.0 Mobile Safari/537.36"
)


class TestMailClientPrefetch:
    def test_msoffice_16_outlook_desktop(self):
        is_bot, reason = detect_bot(OUTLOOK_MSOFFICE_16)
        assert is_bot is True
        assert reason == "mail-client:outlook-desktop-prefetch"

    def test_msoffice_rmj(self):
        is_bot, reason = detect_bot(MSOFFICE_RMJ)
        assert is_bot is True
        assert reason == "mail-client:outlook-desktop-prefetch"

    def test_outlook_trident_msie7(self):
        is_bot, reason = detect_bot(OUTLOOK_TRIDENT)
        assert is_bot is True
        assert reason == "mail-client:outlook-trident-render"

    def test_outlook_msie10_trident(self):
        is_bot, reason = detect_bot(OUTLOOK_MSIE10)
        assert is_bot is True
        # MSIE 10 + Trident hits outlook-trident-render before trident-7
        assert reason in (
            "mail-client:outlook-trident-render",
            "mail-client:trident-7-render",
        )

    def test_gmail_image_proxy(self):
        is_bot, reason = detect_bot(GMAIL_PROXY)
        assert is_bot is True
        assert reason == "mail-client:gmail-image-proxy"


class TestOpenExemptions:
    """Opens through the Gmail/Yahoo image proxy are real user opens; the
    prefetch at delivery time is filtered by the minimum delta gate, not here."""

    def test_gmail_image_proxy_open_is_not_bot(self):
        is_bot, reason = detect_bot(GMAIL_PROXY, event_type="open")
        assert is_bot is False
        assert reason is None

    def test_gmail_image_proxy_click_is_bot(self):
        is_bot, reason = detect_bot(GMAIL_PROXY, event_type="click")
        assert is_bot is True
        assert reason == "mail-client:gmail-image-proxy"

    def test_gmail_proxy_ua_does_not_fall_through_to_eol_rule(self):
        # The GoogleImageProxy UA contains "Windows NT 5.1": the exemption must
        # short-circuit before the eol:windows-xp-or-2000 rule.
        is_bot, _ = detect_bot(GMAIL_PROXY, event_type="open")
        assert is_bot is False

    def test_google_proxy_ip_open_is_not_bot(self):
        is_bot, reason = detect_bot(
            REAL_GMAIL_WEB_CHROME, ip_address="66.102.0.10", event_type="open"
        )
        assert is_bot is False
        assert reason is None

    def test_google_proxy_ip_click_is_bot(self):
        is_bot, reason = detect_bot(
            REAL_GMAIL_WEB_CHROME, ip_address="66.102.0.10", event_type="click"
        )
        assert is_bot is True
        assert reason == "ip:google:proxy"

    def test_apple_mpp_ip_open_still_bot(self):
        # MPP is a prefetch (download != read): still a bot for opens too.
        is_bot, reason = detect_bot(
            REAL_IPHONE, ip_address="17.0.0.10", event_type="open"
        )
        assert is_bot is True
        assert reason == "ip:apple:mpp"

    def test_http_library_open_still_bot(self):
        is_bot, reason = detect_bot(PYTHON_REQUESTS, event_type="open")
        assert is_bot is True
        assert reason == "http-library"


class TestForgedTemplates:
    def test_chrome_future_165(self):
        is_bot, reason = detect_bot(CHROME_FUTURE_165)
        assert is_bot is True
        assert reason == "forged:chrome-future-version"

    def test_firefox_110_selenium(self):
        is_bot, reason = detect_bot(WIN81_FIREFOX110)
        assert is_bot is True
        # Win NT 6.3 fires first; that's still a valid bot signal.
        assert reason in (
            "eol:windows-7-or-8",
            "forged:firefox-110-selenium-template",
        )

    def test_eol_firefox_33(self):
        is_bot, reason = detect_bot(EOL_FIREFOX_33)
        assert is_bot is True
        # Win NT 5.1 + FF33 — both signals; either wins.
        assert reason in (
            "eol:windows-xp-or-2000",
            "eol:firefox-pre-40",
        )


class TestTruncatedAndScripts:
    def test_mozilla_5_bare(self):
        is_bot, reason = detect_bot(MOZILLA_TRUNCATED_5)
        assert is_bot is True
        assert reason == "truncated-ua:mozilla-bare"

    def test_mozilla_4_bare(self):
        is_bot, reason = detect_bot(MOZILLA_TRUNCATED_4)
        assert is_bot is True
        assert reason == "truncated-ua:mozilla-bare"

    def test_go_http_client(self):
        is_bot, reason = detect_bot(GO_HTTP_CLIENT)
        assert is_bot is True
        assert reason == "http-library"

    def test_python_requests(self):
        is_bot, reason = detect_bot(PYTHON_REQUESTS)
        assert is_bot is True
        assert reason == "http-library"

    def test_ruby(self):
        is_bot, reason = detect_bot(RUBY_LIB)
        assert is_bot is True
        assert reason == "http-library"


class TestEmptyAndMalformed:
    def test_none(self):
        is_bot, reason = detect_bot(None)
        assert is_bot is True
        assert reason == "empty-user-agent"

    def test_empty(self):
        is_bot, reason = detect_bot("")
        assert is_bot is True
        assert reason == "empty-user-agent"

    def test_whitespace(self):
        is_bot, reason = detect_bot("   ")
        assert is_bot is True
        assert reason == "empty-user-agent"


class TestIPDetection:
    def test_apple_mpp_ip(self):
        is_bot, reason = detect_bot(REAL_IPHONE, ip_address="17.0.0.10")
        assert is_bot is True
        assert reason == "ip:apple:mpp"

    def test_microsoft_safelinks_ip(self):
        is_bot, reason = detect_bot(REAL_GMAIL_WEB_CHROME, ip_address="40.107.0.10")
        assert is_bot is True
        assert reason == "ip:microsoft:safelinks"

    def test_aws_ec2(self):
        is_bot, reason = detect_bot(REAL_GMAIL_WEB_CHROME, ip_address="18.0.0.10")
        assert is_bot is True
        assert reason == "ip:aws:scanner"

    def test_gcp(self):
        is_bot, reason = detect_bot(REAL_GMAIL_WEB_CHROME, ip_address="34.0.0.10")
        assert is_bot is True
        assert reason == "ip:gcp:scanner"

    def test_digitalocean(self):
        is_bot, reason = detect_bot(REAL_GMAIL_WEB_CHROME, ip_address="64.225.0.10")
        assert is_bot is True
        assert reason == "ip:digitalocean:scanner"

    def test_cloudflare_warp(self):
        is_bot, reason = detect_bot(REAL_IPHONE, ip_address="104.28.0.10")
        assert is_bot is True
        assert reason == "ip:cloudflare:warp"

    def test_fastly_cdn(self):
        is_bot, reason = detect_bot(REAL_GMAIL_WEB_CHROME, ip_address="146.75.0.10")
        assert is_bot is True
        assert reason == "ip:fastly:cdn"

    def test_hostroyale(self):
        is_bot, reason = detect_bot(REAL_GMAIL_WEB_CHROME, ip_address="172.111.15.10")
        assert is_bot is True
        assert reason == "ip:hostroyale:scanner"

    def test_brazil_residential_passes(self):
        # An address outside every bot network (RFC 5737) - should NOT flag.
        is_bot, _ = detect_bot(REAL_IPHONE, ip_address="198.51.100.94")
        assert is_bot is False

    def test_ipv6_does_not_crash(self):
        is_bot, _ = detect_bot(REAL_IPHONE, ip_address="2804::1")
        assert is_bot is False

    def test_malformed_ip_does_not_crash(self):
        is_bot, _ = detect_bot(REAL_IPHONE, ip_address="not-an-ip")
        assert is_bot is False


class TestHumans:
    def test_real_iphone(self):
        is_bot, reason = detect_bot(REAL_IPHONE)
        assert is_bot is False
        assert reason is None

    def test_real_chrome(self):
        is_bot, reason = detect_bot(REAL_GMAIL_WEB_CHROME)
        assert is_bot is False
        assert reason is None

    def test_real_thunderbird(self):
        # Thunderbird desktop — note: client is mail reader. Should pass as human.
        is_bot, reason = detect_bot(REAL_THUNDERBIRD)
        assert is_bot is False
        assert reason is None

    def test_real_chrome_150_current_stable(self):
        is_bot, reason = detect_bot(REAL_CHROME_150)
        assert is_bot is False
        assert reason is None

    def test_real_crios_150(self):
        is_bot, reason = detect_bot(REAL_CRIOS_150)
        assert is_bot is False
        assert reason is None

    def test_real_firefox_153(self):
        is_bot, reason = detect_bot(REAL_FIREFOX_153)
        assert is_bot is False
        assert reason is None

    def test_real_safari_ios26(self):
        is_bot, reason = detect_bot(REAL_SAFARI_IOS26)
        assert is_bot is False
        assert reason is None

    def test_real_android16_bp2a_build(self):
        is_bot, reason = detect_bot(REAL_ANDROID16_BP2A)
        assert is_bot is False
        assert reason is None


class TestReasonFitsColumn:
    def test_all_canonical_reasons_fit_64_chars(self):
        canonical = [
            "empty-user-agent",
            "mail-client:gmail-image-proxy",
            "mail-client:yahoo-mail-proxy",
            "mail-client:outlook-desktop-prefetch",
            "mail-client:office-suite",
            "mail-client:outlook-trident-render",
            "mail-client:trident-7-render",
            "security-scanner:url-defense",
            "crawler:link-preview",
            "crawler:search-engine",
            "crawler:generic-keyword",
            "crawler:bot-keyword",
            "self-declared:antispam",
            "http-library",
            "headless-browser",
            "truncated-ua:mozilla-bare",
            "forged:android-7-template-NRD90M",
            "forged:galaxy-s3-kitkat-template",
            "forged:crios-56-template",
            "forged:chrome-future-version",
            "forged:crios-future-version",
            "forged:firefox-future-version",
            "forged:safari-future-version",
            "forged:firefox-110-selenium-template",
            "forged:firefox-109-selenium-template",
            "eol:windows-7-or-8",
            "eol:windows-xp-or-2000",
            "eol:android-pre-8",
            "eol:firefox-pre-40",
            "ip:apple:mpp",
            "ip:google:url-preview",
            "ip:google:proxy",
            "ip:microsoft:safelinks",
            "ip:aws:scanner",
            "ip:gcp:scanner",
            "ip:azure:scanner",
            "ip:digitalocean:scanner",
            "ip:hostroyale:scanner",
            "ip:cloudflare:warp",
            "ip:fastly:cdn",
        ]
        for r in canonical:
            assert len(r) <= 64, f"reason too long for VARCHAR(64): {r!r}"
