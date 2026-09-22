"""
Модуль распознавания капчи для main.py
Использование:
    from captcha import solve_captcha
    code = solve_captcha(image_bytes)
"""

from io import BytesIO
import logging

import cv2
import numpy as np
import pytesseract
from PIL import Image

log = logging.getLogger("captcha")

# ===== Настройки OCR =====
WHITELIST = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# Несколько PSM для повышения точности
TESS_CONFIGS = [
    f"--psm 7 -c tessedit_char_whitelist={WHITELIST} --oem 3",
    f"--psm 8 -c tessedit_char_whitelist={WHITELIST} --oem 3",
    f"--psm 13 -c tessedit_char_whitelist={WHITELIST} --oem 3",
]

MIN_LEN = 4
MAX_LEN = 8


# =========================================================
#                   ПРЕДОБРАБОТКА
# =========================================================
def _preprocess(img_bgr: np.ndarray) -> np.ndarray:
    """Чистит фон, удаляет линии, бинаризует."""
    # 1. Апскейл x2
    h, w = img_bgr.shape[:2]
    img = cv2.resize(img_bgr, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)

    # 2. HSV-маска белого
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 170]), np.array([180, 70, 255]))

    # 3. Инверсия
    inv = cv2.bitwise_not(mask)

    # 4. Убираем горизонтальные линии
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    horiz = cv2.morphologyEx(inv, cv2.MORPH_OPEN, horiz_kernel, iterations=1)

    # 5. Убираем вертикальные линии
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    vert = cv2.morphologyEx(inv, cv2.MORPH_OPEN, vert_kernel, iterations=1)

    # 6. Вычитаем линии
    lines = cv2.add(horiz, vert)
    no_lines = cv2.subtract(inv, lines)

    # 7. Размытие
    blur = cv2.GaussianBlur(no_lines, (3, 3), 0)

    # 8. Otsu-бинаризация
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 9. Убираем мелкий шум
    kernel = np.ones((2, 2), np.uint8)
    cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)

    # 10. Финальная бинаризация
    _, cleaned = cv2.threshold(cleaned, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 11. Рамка вокруг (Tesseract любит поля)
    cleaned = cv2.copyMakeBorder(
        cleaned, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=0
    )
    return cleaned


# =========================================================
#                   ОЦЕНКА РЕЗУЛЬТАТА
# =========================================================
def _score(text: str) -> int:
    if not text:
        return -100
    score = 0
    if MIN_LEN <= len(text) <= MAX_LEN:
        score += 10
    elif len(text) < MIN_LEN:
        score -= 20
    else:
        score -= 5
    if text.isalnum():
        score += 5
    for i in range(1, len(text)):
        if text[i] == text[i - 1]:
            score -= 3
    return score


# =========================================================
#                   ГЛАВНАЯ ФУНКЦИЯ
# =========================================================
def solve_captcha(image_bytes: bytes) -> str:
    """Принимает байты картинки → возвращает распознанный код."""
    try:
        pil_img = Image.open(BytesIO(image_bytes)).convert("RGB")
        img = np.array(pil_img)
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

        cleaned = _preprocess(img)

        candidates = []
        for cfg in TESS_CONFIGS:
            try:
                text = pytesseract.image_to_string(cleaned, config=cfg)
                text = text.strip().replace(" ", "").replace("\n", "").replace("\t", "")
                if text:
                    candidates.append(text)
            except Exception as e:
                log.debug(f"Tesseract config error: {e}")
                continue

        if not candidates:
            return ""

        unique = list(set(candidates))
        unique.sort(key=_score, reverse=True)
        return unique[0]

    except Exception as e:
        log.warning(f"OCR error: {e}")
        return ""
