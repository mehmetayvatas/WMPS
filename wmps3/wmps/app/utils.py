"""
Utility functions and centralized logger for WMPS.
"""

import logging
import os

# Ensure logs directory exists
LOG_DIR = "/config/WMPS/logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "wmps.log")

# Configure logger
logger = logging.getLogger("WMPS")
logger.setLevel(logging.INFO)

# File handler
fh = logging.FileHandler(LOG_FILE)
fh.setLevel(logging.INFO)
fh_formatter = logging.Formatter("%(asctime)s [%(levelname)s] [WMPS] %(message)s")
fh.setFormatter(fh_formatter)

# Console handler
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch_formatter = logging.Formatter("%(asctime)s [%(levelname)s] [WMPS] %(message)s")
ch.setFormatter(ch_formatter)

# Avoid duplicate handlers
if not logger.handlers:
    logger.addHandler(fh)
    logger.addHandler(ch)

def get_logger():
    """Return the WMPS logger."""
    return logger
