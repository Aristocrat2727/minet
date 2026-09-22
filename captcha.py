"""
Распознавание капчи для worker.py
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

# Несколько PSM для разных случаев
TESS_CONFIGS = [
    f"--psm 7 -c tessedit_char_whitelist={WHITELIST} --oem 3",
    f"--psm 8 -c tessedit_char_whitelist={WHITELIST} --oem 3",
    f"--psm 13 -c tessedit_char_whitelist={WHITELIST} --oem 3",
]

MIN_LEN = 4  # минимальная длина кода
MAX_LEN = 8  # максимальная длина кода


# =========================================================
#                   ПРЕДОБРАБОТКА
# =========================================================
def _preprocess(img_bgr: np.ndarray) -> np.ndarray:
    """Чистит фон, удаляет линии, бинаризует."""
    # 1. Апскейл x2 — Tesseract лучше читает крупное
    h, w = img_bgr.shape[:2]
    img = cv2.resize(img_bgr, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)

    # 2. HSV-маска белого (белый прямоугольник с текстом)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 170]), np.array([180, 70, 255]))

    # 3. Инверсия: текст белый на чёрном
    inv = cv2.bitwise_not(mask)

    # 4. Удаление длинных горизонтальных линий
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    horiz = cv2.morphologyEx(inv, cv2.MORPH_OPEN, horiz_kernel, iterations=1)

    # 5. Удаление длинных вертикальных линий
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    vert = cv2.morphologyEx(inv, cv2.MORPH_OPEN, vert_kernel, iterations=1)

    # 6. Вычитание линий
    lines = cv2.add(horiz, vert)
    no_lines = cv2.subtract(inv, lines)

    # 7. Лёгкое размытие
    blur = cv2.GaussianBlur(no_lines, (3, 3), 0)

    # 8. Otsu-бинаризация
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 9. Убираем мелкие точки
    kernel = np.ones((2, 2), np.uint8)
    cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)

    # 10. Финальная бинаризация
    _, cleaned = cv2.threshold(cleaned, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 11. Рамка вокруг — Tesseract любит поля
    cleaned = cv2.copyMakeBorder(
        cleaned, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=0
    )
    return cleaned


# =========================================================
#                   ОЦЕНКА РЕЗУЛЬТАТА
# =========================================================
def _score(text: str) -> int:
    """Чем выше — тем правдоподобнее код."""
    if not text:
        return -100

    score = 0

    # Длина
    if MIN_LEN <= len(text) <= MAX_LEN:
        score += 10
    elif len(text) < MIN_LEN:
        score -= 20
    else:
        score -= 5

    # Только буквы/цифры
    if text.isalnum():
        score += 5

    # Штраф за повторы подряд (aa, bb, 11)
    for i in range(1, len(text)):
        if text[i] == text[i - 1]:
            score -= 3

    return score


# =========================================================
#                   ГЛАВНАЯ ФУНКЦИЯ
# =========================================================
def solve_captcha(image_bytes: bytes) -> str:
    """
    Принимает байты картинки → возвращает распознанный код.
    Если не удалось — пустую строку.
    """
    try:
        # Загружаем картинку
        pil_img = Image.open(BytesIO(image_bytes)).convert("RGB")
        img = np.array(pil_img)
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

        # Предобработка
        cleaned = _preprocess(img)

        # Пробуем несколько PSM-режимов
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
            log.debug("OCR: пусто во всех режимах")
            return ""

        # Уникализируем и сортируем по score
        unique = list(set(candidates))
        unique.sort(key=_score, reverse=True)
        best = unique[0]

        # Отсекаем слишком короткие/длинные
        if len(best) < MIN_LEN or len(best) > MAX_LEN:
            log.debug(f"OCR: {best!r} — длина вне [{MIN_LEN},{MAX_LEN}]")
            # но всё равно вернём, вдруг поможет
            return best

        return best

    except Exception as e:
        log.warning(f"OCR error: {e}")
        return ""


# =========================================================
#                   ДИАГНОСТИКА
# =========================================================
def solve_captcha_verbose(image_bytes: bytes) -> dict:
    """
    Расширенная версия для отладки.
    Возвращает: {'best': ..., 'all': [...], 'scores': [...]}
    """
    try:
        pil_img = Image.open(BytesIO(image_bytes)).convert("RGB")
        img = np.array(pil_img)
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        cleaned = _preprocess(img)

        results = []
        for i, cfg in enumerate(TESS_CONFIGS):
            try:
                text = pytesseract.image_to_string(cleaned, config=cfg)
                text = text.strip().replace(" ", "").replace("\n", "")
                results.append((f"psm{i}", text, _score(text)))
            except Exception as e:
                results.append((f"psm{i}", f"error: {e}", -999))

        # Лучший по score
        valid = [(name, text, sc) for name, text, sc in results if text and not text.startswith("error")]
        valid.sort(key=lambda x: x[2], reverse=True)
        best = valid[0][1] if valid else ""

        return {
            "best": best,
            "all": results,
            "preprocessed_shape": cleaned.shape,
        }
    except Exception as e:
        return {"best": "", "error": str(e)}


if __name__ == "__main__":
    # Быстрый тест: python captcha.py path/to/image.png
    import sys
    if len(sys.argv) > 1:
        with open(sys.argv[1], "rb") as f:
            data = f.read()
        print("Best:", solve_captcha(data))
        print("Verbose:", solve_captcha_verbose(data))
    else:
        print("Usage: python captcha.py <image.png>")
