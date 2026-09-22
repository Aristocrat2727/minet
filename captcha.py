from io import BytesIO
import cv2
import numpy as np
import pytesseract
from PIL import Image

WHITELIST = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
TESS_CONFIG = f"--psm 7 -c tessedit_char_whitelist={WHITELIST}"


def solve_captcha(image_bytes: bytes) -> str:
    img = np.array(Image.open(BytesIO(image_bytes)).convert("RGB"))
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([0, 0, 180]), np.array([180, 60, 255]))
    inv = cv2.bitwise_not(mask)

    blur = cv2.GaussianBlur(inv, (3, 3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8), iterations=1)

    text = pytesseract.image_to_string(cleaned, config=TESS_CONFIG)
    return text.strip().replace(" ", "").replace("\n", "")
