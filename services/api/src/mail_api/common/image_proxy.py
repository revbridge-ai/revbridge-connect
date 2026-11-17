"""
Image Proxy Integration for Email Templates

Processes HTML templates to use the installation's image proxy service,
which handles transparent PNG images by applying appropriate backgrounds.

This solves the "black logo" problem in email clients where transparent
PNGs are rendered with black backgrounds instead of the intended color.
Every wrapped URL is signed (HMAC-SHA256 with IMAGE_PROXY_SECRET): the proxy
only fetches images that this installation put in an email.
"""

import base64
import hashlib
import hmac
import logging
import os
import re
from urllib.parse import quote

import httpx

# Configuration
# Public URL of the proxy (https://img.<domain>), the one used in the emails' <img> tags
IMAGE_PROXY_URL = (os.getenv("IMAGE_PROXY_URL") or "").rstrip("/")
# Internal address for POST /svg (the API reaches the proxy over the compose network)
IMAGE_PROXY_INTERNAL_URL = (os.getenv("IMAGE_PROXY_INTERNAL_URL") or "http://image-proxy:8080").rstrip("/")
IMAGE_PROXY_SECRET = os.getenv("IMAGE_PROXY_SECRET", "")
# Without a public URL or a secret there is nothing to wrap (the proxy rejects unsigned URLs)
IMAGE_PROXY_ENABLED = (
    os.getenv("IMAGE_PROXY_ENABLED", "true").lower() == "true"
    and bool(IMAGE_PROXY_URL)
    and bool(IMAGE_PROXY_SECRET)
)


def sign_proxy_url(url: str, bg: str) -> str:
    """HMAC da URL e da cor; o image proxy confere (services/image-proxy/main.py: sign)."""
    mac = hmac.new(IMAGE_PROXY_SECRET.encode(), f"{url}\n{bg.upper()}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:16]).decode().rstrip("=")


def extract_background_color(style: str) -> str | None:
    """Extract background-color from inline style string."""
    # Match hex color
    match = re.search(r'background-color:\s*#?([0-9A-Fa-f]{3,6})', style, re.IGNORECASE)
    if match:
        color = match.group(1).upper()
        # Normalize 3-char to 6-char
        if len(color) == 3:
            color = ''.join([c * 2 for c in color])
        return color

    # Match rgb()
    match = re.search(r'background-color:\s*rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', style, re.IGNORECASE)
    if match:
        r, g, b = int(match.group(1)), int(match.group(2)), int(match.group(3))
        return f'{r:02X}{g:02X}{b:02X}'

    # Match color names (common ones)
    color_map = {
        'white': 'FFFFFF',
        'black': '000000',
        'red': 'FF0000',
        'green': '00FF00',
        'blue': '0000FF',
        'transparent': 'FFFFFF',  # Default to white for transparent
    }

    match = re.search(r'background-color:\s*(\w+)', style, re.IGNORECASE)
    if match and match.group(1).lower() in color_map:
        return color_map[match.group(1).lower()]

    return None


def find_background_colors(html: str) -> dict:
    """Find all background colors in the HTML document with their positions."""
    colors = {}

    # Match style attribute with background-color
    style_regex = re.compile(
        r'<(?:td|div|table|tr|body)[^>]*style=["\']([^"\']*background-color:[^"\']+)["\'][^>]*>',
        re.IGNORECASE
    )
    for match in style_regex.finditer(html):
        color = extract_background_color(match.group(1))
        if color:
            colors[match.start()] = color

    # Match bgcolor attribute (legacy HTML)
    bgcolor_regex = re.compile(
        r'<(?:td|div|table|tr|body)[^>]*bgcolor=["\']#?([0-9A-Fa-f]{3,6})["\']',
        re.IGNORECASE
    )
    for match in bgcolor_regex.finditer(html):
        color = match.group(1).upper()
        if len(color) == 3:
            color = ''.join([c * 2 for c in color])
        colors[match.start()] = color

    return colors


def find_nearest_background_color(position: int, colors: dict, default: str = 'FFFFFF') -> str:
    """Find the nearest parent background color for a given position."""
    nearest_pos = -1
    nearest_color = default

    for pos, color in colors.items():
        if pos < position and pos > nearest_pos:
            nearest_pos = pos
            nearest_color = color

    return nearest_color


def should_process_url(url: str) -> bool:
    """Check if a URL should be processed by the proxy."""
    # Skip data URIs
    if url.startswith('data:'):
        return False

    # Skip relative URLs
    if not url.startswith('http://') and not url.startswith('https://'):
        return False

    # Skip if already proxied
    if IMAGE_PROXY_URL in url:
        return False

    # RevBridge Connect: any image proxy URL (from another installation or environment) is
    # already wrapped; wrapping it again would chain proxies (img.../p?url=img.../p?url=...).
    if "/p?url=" in url:
        return False

    return True


def generate_proxy_url(original_url: str, bg_color: str = 'FFFFFF') -> str:
    """Generate a signed proxy URL for an image."""
    bg_color = (bg_color or 'FFFFFF').lstrip('#').upper()
    encoded_url = quote(original_url, safe='')
    sig = sign_proxy_url(original_url, bg_color)
    return f"{IMAGE_PROXY_URL}/p?url={encoded_url}&bg={bg_color}&s={sig}"


def process_html_images(html: str, default_bg: str = 'FFFFFF') -> str:
    """
    Process all images in HTML to use the image proxy.

    This function:
    1. Finds all <img> tags with external URLs
    2. Extracts the background color from parent elements
    3. Replaces URLs with proxy URLs

    Args:
        html: The HTML template content
        default_bg: Default background color if none found (default: FFFFFF)

    Returns:
        HTML with image URLs replaced by proxy URLs
    """
    if not IMAGE_PROXY_ENABLED:
        return html

    # Find all background colors in the document
    bg_colors = find_background_colors(html)

    # Process all <img> tags
    img_regex = re.compile(r'<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE)

    def replace_img(match):
        full_tag = match.group(0)
        src = match.group(1)

        # Check if we should process this URL
        if not should_process_url(src):
            return full_tag

        # Find the nearest background color
        bg_color = find_nearest_background_color(match.start(), bg_colors, default_bg)

        # Generate proxy URL
        proxy_url = generate_proxy_url(src, bg_color)

        # Replace src in the tag
        return re.sub(
            r'src=["\'][^"\']+["\']',
            f'src="{proxy_url}"',
            full_tag,
            flags=re.IGNORECASE
        )

    return img_regex.sub(replace_img, html)


logger = logging.getLogger(__name__)


async def process_html_inline_svgs(html: str, default_bg: str = 'FFFFFF') -> str:
    """
    Replace inline <svg>...</svg> elements with <img> tags pointing to the proxy.

    POSTs each SVG to the /svg endpoint which converts to PNG and returns a short serve URL.
    This is needed because most email clients strip inline SVG elements.
    """
    svg_pattern = re.compile(r'<svg\s[^>]*>.*?</svg>', re.IGNORECASE | re.DOTALL)

    matches = list(svg_pattern.finditer(html))
    if not matches:
        return html

    replacements = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for match in matches:
            svg_tag = match.group(0)

            w_match = re.search(r'\bwidth=["\'](\d+)', svg_tag)
            h_match = re.search(r'\bheight=["\'](\d+)', svg_tag)
            width = w_match.group(1) if w_match else None
            height = h_match.group(1) if h_match else None

            params = {"bg": default_bg}
            if width:
                params["w"] = width
            if height:
                params["h"] = height

            try:
                resp = await client.post(
                    f"{IMAGE_PROXY_INTERNAL_URL}/svg",
                    content=svg_tag.encode("utf-8"),
                    params=params,
                    headers={
                        "Content-Type": "image/svg+xml",
                        "Authorization": f"Bearer {IMAGE_PROXY_SECRET}",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                serve_url = data["url"]
            except Exception as e:
                logger.warning(f"Failed to upload SVG to proxy: {e}")
                replacements.append((match.start(), match.end(), svg_tag))
                continue

            img_attrs = f'src="{serve_url}"'
            if width:
                img_attrs += f' width="{width}"'
            if height:
                img_attrs += f' height="{height}"'
            img_attrs += ' style="display:block;"'

            replacements.append((match.start(), match.end(), f"<img {img_attrs}>"))

    # Apply replacements in reverse order
    result = html
    for start, end, replacement in reversed(replacements):
        result = result[:start] + replacement + result[end:]

    return result


async def maybe_process_html_images(html: str, default_bg: str = 'FFFFFF') -> str:
    """
    Conditionally process HTML images based on configuration.

    Args:
        html: The HTML template content
        default_bg: Default background color if none found

    Returns:
        HTML with image URLs replaced by proxy URLs (if enabled)
    """
    if not IMAGE_PROXY_ENABLED:
        return html

    # First: convert inline SVGs to <img> tags (email clients don't support inline SVG)
    html = await process_html_inline_svgs(html, default_bg)

    # Then: process all <img> tags through the proxy
    return process_html_images(html, default_bg)
