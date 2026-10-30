"""Internal browser renderer. No public port, redirects and requests checked."""
import asyncio
import ipaddress
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from playwright.async_api import async_playwright


def public_url(url):
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return False
    try:
        addr = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
        return bool(addr) and all(ipaddress.ip_address(a[4][0]).is_global for a in addr)
    except (OSError, ValueError):
        return False


async def render(url):
    if not await asyncio.to_thread(public_url, url):
        raise ValueError("Non-public URL rejected")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(accept_downloads=False, service_workers="block")
        page = await context.new_page()
        async def route_check(route):
            if await asyncio.to_thread(public_url, route.request.url):
                await route.continue_()
            else:
                await route.abort()
        await page.route("**/*", route_check)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(1800)
            if response and response.status in (403, 429):
                raise ValueError("Access restricted")
            final_url = page.url
            if not await asyncio.to_thread(public_url, final_url):
                raise ValueError("Non-public redirect rejected")
            html = await page.content()
            if len(html.encode("utf-8")) > 3_000_000:
                raise ValueError("Rendered document too large")
            return {"url": final_url, "html": html}
        finally:
            await browser.close()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/render":
            self.send_error(404)
            return
        try:
            count = int(self.headers.get("Content-Length", "0"))
            if count <= 0 or count > 8192:
                raise ValueError("Invalid request size")
            url = json.loads(self.rfile.read(count))["url"]
            data = asyncio.run(render(url))
            content = json.dumps(data).encode()
            self.send_response(200)
        except Exception as exc:
            content = json.dumps({"error": type(exc).__name__}).encode()
            self.send_response(422)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8765), Handler).serve_forever()
