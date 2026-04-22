import base64
import datetime
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config.settings import settings

logger = logging.getLogger("kalshi.client")


class KalshiAuthError(Exception):
    pass


class KalshiAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message}")


class KalshiClient:
    def __init__(self):
        self._private_key = None
        self._base_url = settings.base_url

    def _load_key(self):
        if self._private_key is not None:
            return
        key_path = Path(settings.kalshi_private_key_path)
        if not key_path.exists():
            raise KalshiAuthError(
                f"Private key not found at {key_path}. "
                "Set KALSHI_PRIVATE_KEY_PATH in .env and place your key file there."
            )
        with open(key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )

    def _sign_request(self, method: str, path: str) -> dict:
        self._load_key()
        timestamp_ms = int(datetime.datetime.now().timestamp() * 1000)
        path_no_query = urlparse(path).path
        msg = f"{timestamp_ms}{method.upper()}{path_no_query}"
        signature = self._private_key.sign(
            msg.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": settings.kalshi_api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, params: dict = None, body: dict = None) -> dict:
        url = self._base_url + path
        headers = self._sign_request(method, path)
        delay = 1.0

        for attempt in range(5):
            try:
                logger.debug("%s %s", method, url)
                with httpx.Client(timeout=30.0) as client:
                    response = client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=body,
                    )

                if response.status_code in (429, 503, 502, 504):
                    logger.warning("Transient %d on %s %s, backing off %.1fs", response.status_code, method, path, delay)
                    time.sleep(delay)
                    delay = min(delay * 2, 60.0)
                    headers = self._sign_request(method, path)
                    continue

                if response.status_code == 401:
                    raise KalshiAuthError("Authentication failed. Check KALSHI_API_KEY_ID and private key.")

                if not response.is_success:
                    raise KalshiAPIError(response.status_code, response.text[:200])

                return response.json() if response.content else {}

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt == 4:
                    raise
                logger.warning("Network error attempt %d: %s. Retrying in %.1fs", attempt + 1, e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60.0)

        raise KalshiAPIError(429, "Max retries exceeded")

    def get(self, path: str, params: dict = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict = None) -> dict:
        return self._request("POST", path, body=body)

    def delete(self, path: str) -> dict:
        return self._request("DELETE", path)
