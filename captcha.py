from io import BytesIO
import logging

import cv2
import numpy as np
import pytesseract
from PIL import Image

log = logging.getLogger("captcha")

WHITELIST = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# ⚡ Один PSM — быстрее в 3 раза
TESS_CONFIG = f"--psm 7 -c tessedit_char_whitelist={WHITELIST} --oem 3"

MIN_LEN = 4
MAX_LEN = 8


def _preprocess_fast(img_bgr: np.ndarray) -> np.ndarray:
    """⚡ Быстрая предобработка — без апскейла, без морфологии линий."""
    # 1. HSV-маска белого (без апскейла — экономим время)
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 170]), np.array([180, 70, 255]))
    inv = cv2.bitwise_not(mask)

    # 2. Otsu-бинаризация
    _, thresh = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 3. Убираем мелкий шум (маленькое ядро — быстро)
    kernel = np.ones((2, 2), np.uint8)
    cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)

    # 4. Рамка вокруг (Tesseract любит поля)
    cleaned = cv2.copyMakeBorder(cleaned, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=0)
    return cleaned


def solve_captcha(image_bytes: bytes) -> str:
    """⚡ Быстрое распознавание капчи."""
    try:
        pil_img = Image.open(BytesIO(image_bytes)).convert("L")  # сразу grayscale
        img = np.array(pil_img)
        img_bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

        cleaned = _preprocess_fast(img_bgr)

        text = pytesseract.image_to_string(cleaned, config=TESS_CONFIG)
        text = text.strip().replace(" ", "").replace("\n", "").replace("\t", "")

        # Отсекаем явно мусорные результаты
        if len(text) < MIN_LEN or len(text) > MAX_LEN:
            return text  # всё равно вернём — пусть worker решает

        return text

    except Exception as e:
        log.warning(f"OCR error: {e}")
        return ""
