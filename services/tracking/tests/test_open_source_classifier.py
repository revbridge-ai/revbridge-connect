"""classify_open_source: Apple's privacy proxy vs a person reading on an Apple device."""

from mail_tracking.app.tracking.routers.tracking_router import classify_open_source

IP = "203.0.113.20"

APPLE_MAIL = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)"
CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SAFARI_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Safari/605.1.15"
)
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1"
)
CHROME_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) CriOS/131.0.6778.73 Mobile/15E148 Safari/604.1"
)
GMAIL_PROXY = "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 (via ggpht.com GoogleImageProxy)"


def test_apple_mail_agent_is_the_proxy():
    assert classify_open_source(APPLE_MAIL, IP) == "apple_mpp"


def test_browsers_on_apple_devices_are_people():
    for ua in (CHROME_MAC, SAFARI_MAC, SAFARI_IPHONE, CHROME_IPHONE):
        assert classify_open_source(ua, IP) == "direct", ua


def test_gmail_proxy_is_still_a_proxy():
    assert classify_open_source(GMAIL_PROXY, IP) == "gmail_proxy"
