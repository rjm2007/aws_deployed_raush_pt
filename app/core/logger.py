import logging
import os

os.makedirs("logs", exist_ok=True)

logger = logging.getLogger("tebra_debug")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler("logs/tebra_debug.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        "%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)
