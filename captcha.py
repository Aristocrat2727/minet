"""
Улучшенное распознавание капчи.
- Апскейл x2
- Удаление фона через HSV
- Удаление линий (гориз. + верт. морфология)
- Otsu-бинаризация
- Несколько PSM-режимов Tesseract
- Выбор лучшего результата
"""

from io import BytesIO
import cv2
import numpy as np
import pytesseract
from PIL import Image

WHITELIST = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# Несколько PSM для разных случаев
TESS_CONFIGS = [
    f"--psm 7 -c tessedit_char_whitelist={WHITELIST} --oem 3",
    f"--psm 8 -c tessedit_char_whitelist={WHITELIST} --oem 3",
    f"--psm 13 -c tessedit_char_whitelist={WHITELIST} --oem 3",
]


def _preprocess(img_bgr: np.ndarray) -> np.ndarray:
    """Улучшенная предобработка капчи."""
    # 1. Апскейл x2 — Tesseract лучше читает крупное
    h, w = img_bgr.shape[:2]
    img = cv2.resize(img_bgr, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)

    # 2. Перевод в HSV и маска белого (белый прямоугольник с текстом)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 170]), np.array([180, 70, 255]))

    # 3. Инверсия: текст (чёрный на белом) → белый на чёрном
    inv = cv2.bitwise_not(mask)

    # 4. Удаление длинных горизонтальных линий
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    horiz = cv2.morphologyEx(inv, cv2.MORPH_OPEN, horiz_kernel, iterations=1)

    # 5. Удаление длинных вертикальных линий
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    vert = cv2.morphologyEx(inv, cv2.MORPH_OPEN, vert_kernel, iterations=1)

    # 6. Вычитаем линии из картинки
    lines = cv2.add(horiz, vert)
    no_lines = cv2.subtract(inv, lines)

    # 7. Гауссово размытие (сглаживание шума)
    blur = cv2.GaussianBlur(no_lines, (3, 3), 0)

    # 8. Otsu-бинаризация
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 9. Убираем мелкие точки/шум
    kernel = np.ones((2, 2), np.uint8)
    cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)

    # 10. Финальная бинаризация
    _, cleaned = cv2.threshold(cleaned, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 11. Добавляем рамку — Tesseract любит поля вокруг текста
    cleaned = cv2.copyMakeBorder(cleaned, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=0)

    return cleaned


def _score(text: str) -> int:
    """
    Оценка «правдоподобности» результата OCR.
    Чем больше — тем вероятнее, что это правильный код.
    """
    if not text:
        return -100

    score = 0

    # Длина 4-7 символов — оптимально
    if 4 <= len(text) <= 7:
        score += 10
    elif len(text) < 4:
        score -= 10
    else:
        score -= 5

    # Только буквы и цифры — уже отфильтровано whitelist-ом, но на всякий
    if text.isalnum():
        score += 5

    # Нет повторяющихся символов подряд (bbb, 111) — хорошо
    repeats = 0
    for i in range(1, len(text)):
        if text[i] == text[i - 1]:
            repeats += 1
    score -= repeats * 3

    return score


def solve_captcha(image_bytes: bytes) -> str:
    """Распознаёт капчу. Возвращает строку или пустую строку."""
    try:
        img = np.array(Image.open(BytesIO(image_bytes)).convert("RGB"))
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
            except Exception:
                continue

        if not candidates:
            return ""

        # Уникализируем и выбираем лучший по score
        unique = list(set(candidates))
        unique.sort(key=_score, reverse=True)

        return unique[0]

    except Exception as e:
        import logging
        logging.getLogger("captcha").warning(f"OCR error: {e}")
        return ""
