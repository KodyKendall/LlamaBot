try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
from bs4 import BeautifulSoup
import re
import asyncio

async def capture_page_and_img_src(url: str, image_path: str) -> tuple[str, list[str]]:
    if not PLAYWRIGHT_AVAILABLE:
        print("Warning: Playwright is not installed. Screenshot functionality is disabled.")
        print("To enable screenshots, install playwright: pip install playwright && playwright install")
        return "", []

    browser = None  # Track browser for cleanup
    page = None     # Track page for cleanup
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            await page.goto(url)
            await page.screenshot(path=image_path, full_page=True)
            print(f"Screenshot saved to {image_path}")

            html = await page.content()

            image_sources = await page.query_selector_all('img')
            image_sources = [await img.get_attribute('src') for img in image_sources]
            print(f"Image sources: {image_sources}")

            # Explicit cleanup to prevent zombie processes
            trimmed_html = trim_html_for_llm(html)

            await page.close()
            await browser.close()

            return trimmed_html, image_sources
    except Exception as e:
        print(f"Error capturing screenshot: {str(e)}")
        # Ensure cleanup even on error (prevents zombie processes)
        try:
            if page:
                await page.close()
        except Exception as cleanup_err:
            print(f"Error closing page: {cleanup_err}")
        try:
            if browser:
                await browser.close()
        except Exception as cleanup_err:
            print(f"Error closing browser: {cleanup_err}")
        return "", []
    
def trim_html_for_llm(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')

    for tag_name in ['script', 'meta', 'noscript', 'iframe', 'svg', 'canvas', 'video', 'audio', 'link', 'style', 'class']:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # 3. Only allow a minimal set of attributes
    allowed_attrs = {"src", "href", "alt", "title"}

    for tag in soup.find_all(True):
        original_attributes = list(tag.attrs.keys())

        for attr in original_attributes:
            if attr not in allowed_attrs:
                del tag[attr]

    return str(soup)


# python agents/utils/playwright_screenshot.py
if __name__ == "__main__":
    trimmed_html, image_sources = asyncio.run(capture_page_and_img_src("https://www.google.com", "assets/google-screenshot.png"))
    print(trimmed_html)
    print(image_sources)
