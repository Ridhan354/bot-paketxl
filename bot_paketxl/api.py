"""HTTP client for XL package API."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple
import json
import logging
import time

import requests

LIMIT_PHRASE = "batas maksimal pengecekan"
BLOCK_SECONDS = 3 * 3600


@dataclass
class ApiResult:
    success: bool
    payload: Optional[dict]
    message: str
    block_seconds: int = 0


class XLApiClient:
    def __init__(self, template: str, timeout: int) -> None:
        self.template = template
        self.timeout = timeout

    def fetch(self, number: str) -> ApiResult:
        url = self.template.format(number=number)
        try:
            response = requests.get(url, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logging.exception("Failed to fetch data for %s", number)
            return ApiResult(False, None, f"Gagal mengambil data: {exc}")

        if not payload.get("success"):
            message = payload.get("message") or payload.get("data", {}).get("package_info", {}).get("error_message") or "Unknown error"
            block = BLOCK_SECONDS if message and LIMIT_PHRASE in message.lower() else 0
            return ApiResult(False, payload, message, block)

        err_msg = (payload.get("data", {}).get("package_info", {}) or {}).get("error_message")
        if err_msg:
            block = BLOCK_SECONDS if err_msg and LIMIT_PHRASE in err_msg.lower() else 0
            return ApiResult(False, payload, err_msg, block)

        return ApiResult(True, payload, "")


__all__ = ["XLApiClient", "ApiResult", "BLOCK_SECONDS"]
