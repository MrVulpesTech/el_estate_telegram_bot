"""
Scraping and image processing service using remote Selenium and aiohttp.
Extracts image URLs from OLX/Otodom, downloads images, and crops a bottom area.
"""

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import List, Tuple

import aiohttp
from aiolimiter import AsyncLimiter
from PIL import Image
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


MAX_CONNECTIONS = 100
DEFAULT_CROP_PERCENTAGE = 15
RATE_LIMIT_RPS = int(os.getenv("RATE_LIMIT_RPS", "5"))
_limiter = AsyncLimiter(RATE_LIMIT_RPS, time_period=1)

_executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=10)


def _remove_watermark(image_path: str, output_path: str, crop_percent: int = DEFAULT_CROP_PERCENTAGE) -> None:
    img = Image.open(image_path)
    width, height = img.size
    new_height = int(height * (100 - crop_percent) / 100)
    crop_area = (0, 0, width, new_height)
    img.crop(crop_area).save(output_path)


def _browser_scrape(url: str, selenium_url: str) -> List[str]:
    driver = None
    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=412,915")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Linux; Android 10; SM-G975F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Mobile Safari/537.36"
        )

        driver = webdriver.Remote(command_executor=selenium_url, options=options)
        driver.get(url)

        # Try dismissing cookie consent but do not fail if not present
        try:
            WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, "//div[contains(text(),'Прийняти всі')]"))
            ).click()
        except Exception:
            pass

        image_urls: List[str] = []
        if "otodom" in url:
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='carousel-container']"))
                )
                carousel = driver.find_element(By.CSS_SELECTOR, "[data-testid='carousel-container']")
                for img in carousel.find_elements(By.TAG_NAME, "img"):
                    src = img.get_attribute("src")
                    if not src:
                        continue
                    if "/image;" in src:
                        src = src.split("/image;")[0] + "/image;"
                    image_urls.append(src)
            except Exception:
                pass
        elif "olx" in url:
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "swiper-slide"))
                )
                for div in driver.find_elements(By.CLASS_NAME, "swiper-slide"):
                    try:
                        img = div.find_element(By.TAG_NAME, "img")
                        src = img.get_attribute("src")
                        if src:
                            image_urls.append(src)
                    except Exception:
                        continue
            except Exception:
                pass

        return image_urls
    finally:
        if driver:
            driver.quit()


async def _fetch_image(session: aiohttp.ClientSession, url: str) -> bytes:
    async with _limiter:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
    return b""


async def _process_image(
    session: aiohttp.ClientSession,
    img_url: str,
    index: int,
    user_dir: str,
    crop_percent: int,
) -> str:
    content = await _fetch_image(session, img_url)
    if not content:
        return ""
    image_path = os.path.join(user_dir, f"image_{index+1}.png")
    cropped_path = os.path.join(user_dir, f"cropped_image_{index+1}.png")
    Image.open(BytesIO(content)).save(image_path)
    _remove_watermark(image_path, cropped_path, crop_percent)
    return cropped_path if os.path.exists(cropped_path) else image_path


async def scrape_images(
    url: str,
    user_id: int,
    selenium_url: str,
    *,
    crop_percent: int = DEFAULT_CROP_PERCENTAGE,
) -> Tuple[List[str], str]:
    # Create per-user temp directory
    user_dir = f"images/{user_id}_{int(time.time())}"
    os.makedirs(user_dir, exist_ok=True)

    loop = asyncio.get_running_loop()
    image_urls = await loop.run_in_executor(_executor, _browser_scrape, url, selenium_url)
    if not image_urls:
        return [], user_dir

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=MAX_CONNECTIONS)) as session:
        tasks = [
            asyncio.create_task(_process_image(session, img_url, i, user_dir, crop_percent))
            for i, img_url in enumerate(image_urls)
        ]
        processed = await asyncio.gather(*tasks)

    valid = [p for p in processed if p]
    return valid, user_dir


