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

import logging
import aiohttp
from aiolimiter import AsyncLimiter
from PIL import Image
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import WebDriverException
from aiohttp import ClientTimeout

MAX_CONNECTIONS = 100
DEFAULT_CROP_PERCENTAGE = 15
RATE_LIMIT_RPS = int(os.getenv("RATE_LIMIT_RPS", "5"))
_limiter = AsyncLimiter(RATE_LIMIT_RPS, time_period=1)

SCRAPE_RETRIES = int(os.getenv("SCRAPE_RETRIES", "2"))
SCRAPE_RETRY_BACKOFF_S = float(os.getenv("SCRAPE_RETRY_BACKOFF_S", "2.0"))
IMAGE_FETCH_RETRIES = int(os.getenv("IMAGE_FETCH_RETRIES", "2"))
IMAGE_FETCH_RETRY_BACKOFF_S = float(os.getenv("IMAGE_FETCH_RETRY_BACKOFF_S", "1.0"))

_executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=10)

logger = logging.getLogger(__name__)

def _remove_watermark(
    image_path: str, output_path: str, crop_percent: int = DEFAULT_CROP_PERCENTAGE
) -> None:
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
        try:
            driver.set_page_load_timeout(15)
        except Exception:
            pass
        driver.get(url)

        # Try dismissing cookie consent but do not fail if not present
        try:
            WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//div[contains(text(),'Прийняти всі')]")
                )
            ).click()
        except Exception:
            logger.debug("cookies.dismiss.missing url=%s", url)

        image_urls: List[str] = []
        if "otodom" in url:
            try:
                WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "[data-testid='carousel-container']")
                    )
                )
                carousel = driver.find_element(
                    By.CSS_SELECTOR, "[data-testid='carousel-container']"
                )
                for img in carousel.find_elements(By.TAG_NAME, "img"):
                    src = img.get_attribute("src")
                    if not src:
                        continue
                    if "/image;" in src:
                        src = src.split("/image;")[0] + "/image;"
                    image_urls.append(src)
            except Exception as e:
                logger.warning("otodom.extract.error url=%s err=%s", url, e)
        elif "olx" in url:
            try:
                WebDriverWait(driver, 8).until(
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
            except Exception as e:
                logger.warning("olx.extract.error url=%s err=%s", url, e)

        return image_urls
    finally:
        if driver:
            try:
                driver.quit()
            except WebDriverException:
                logger.warning("browser.quit.error url=%s", url)


def _browser_scrape_with_retry(url: str, selenium_url: str) -> List[str]:
    # Retry when extraction fails or returns no images
    last_exception: Exception | None = None
    for attempt in range(1, SCRAPE_RETRIES + 2):  # N retries means N+1 total attempts
        try:
            images = _browser_scrape(url, selenium_url)
            if images:
                if attempt > 1:
                    logger.info(
                        "extract.recovered url=%s attempts=%d", url, attempt
                    )
                return images
        except Exception as e:
            last_exception = e
            logger.warning(
                "extract.attempt.error url=%s attempt=%d/%d err=%s",
                url,
                attempt,
                SCRAPE_RETRIES + 1,
                e,
            )
        if attempt <= SCRAPE_RETRIES:
            time.sleep(SCRAPE_RETRY_BACKOFF_S * attempt)
    if last_exception:
        logger.warning("extract.failed url=%s err=%s", url, last_exception)
    return []


async def _fetch_image(session: aiohttp.ClientSession, url: str) -> bytes:
    for attempt in range(1, IMAGE_FETCH_RETRIES + 2):
        async with _limiter:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    logger.warning(
                        "image.fetch.non200 status=%d url=%s attempt=%d/%d",
                        resp.status,
                        url,
                        attempt,
                        IMAGE_FETCH_RETRIES + 1,
                    )
            except asyncio.TimeoutError:
                logger.error(
                    "image.fetch.timeout url=%s attempt=%d/%d",
                    url,
                    attempt,
                    IMAGE_FETCH_RETRIES + 1,
                )
            except Exception as e:
                logger.error(
                    "image.fetch.error url=%s err=%s attempt=%d/%d",
                    url,
                    e,
                    attempt,
                    IMAGE_FETCH_RETRIES + 1,
                )
        if attempt <= IMAGE_FETCH_RETRIES:
            await asyncio.sleep(IMAGE_FETCH_RETRY_BACKOFF_S * attempt)
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
    logger.info("scrape.start user_id=%s url=%s crop_percent=%d", user_id, url, crop_percent)
    user_dir = f"images/{user_id}_{int(time.time())}"
    os.makedirs(user_dir, exist_ok=True)

    loop = asyncio.get_running_loop()
    image_urls = await loop.run_in_executor(
        _executor, _browser_scrape_with_retry, url, selenium_url
    )
    if not image_urls:
        logger.warning("scrape.no_images user_id=%s url=%s", user_id, url)
        return [], user_dir

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=MAX_CONNECTIONS),
        timeout=ClientTimeout(total=20, connect=5),
    ) as session:
        tasks = [
            asyncio.create_task(
                _process_image(session, img_url, i, user_dir, crop_percent)
            )
            for i, img_url in enumerate(image_urls)
        ]
        processed = await asyncio.gather(*tasks)

    valid = [p for p in processed if p]
    logger.info(
        "scrape.done user_id=%s url=%s processed=%d/%d",
        user_id,
        url,
        len(valid),
        len(image_urls),
    )
    return valid, user_dir
