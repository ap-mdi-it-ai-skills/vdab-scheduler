from __future__ import annotations

import logging
import time
from typing import Any

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

LOGGER = logging.getLogger(__name__)


def _is_retryable_exception(exc: BaseException) -> bool:
    if not isinstance(exc, requests.exceptions.RequestException):
        return False
    response = getattr(exc, "response", None)
    if response is None:
        return True
    return response.status_code == 429 or response.status_code >= 500


class VdabClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        ibm_client_id: str,
        env: str,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._ibm_client_id = ibm_client_id
        self._token: str | None = None
        self._token_expiry = 0.0
        self._token_url, self._api_base = self._build_urls(env)

    def _build_urls(self, env: str) -> tuple[str, str]:
        if env == "test":
            return (
                "https://op-derden-cbt.vdab.be/isam/sps/oauth/oauth20/token",
                "https://api.vdab.be/services/openservices-test/vacatures/v4",
            )
        return (
            "https://op-derden.vdab.be/isam/sps/oauth/oauth20/token",
            "https://api.vdab.be/services/openservices/vacatures/v4",
        )

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10),
    )
    def get_bearer_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token

        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        response = requests.post(self._token_url, data=payload, headers=headers, timeout=15)
        response.raise_for_status()

        body = response.json()
        self._token = str(body["access_token"])
        expires_in = int(body.get("expires_in", 900))
        self._token_expiry = time.time() + expires_in - 60
        return self._token

    def _headers(self) -> dict[str, str]:
        token = self.get_bearer_token()
        return {
            "Authorization": f"Bearer {token}",
            "X-IBM-Client-Id": self._ibm_client_id,
            "Accept": "application/json",
        }

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10),
    )
    def search_vacancies(self, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._api_base}/vacatures"
        response = requests.get(url, headers=self._headers(), params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    @retry(
        retry=retry_if_exception(_is_retryable_exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10),
    )
    def get_vacancy_detail(self, vacancy_id: str) -> dict[str, Any] | None:
        url = f"{self._api_base}/vacatures/{vacancy_id}"
        response = requests.get(url, headers=self._headers(), timeout=30)
        if response.status_code == 404:
            LOGGER.warning("Vacancy %s not found", vacancy_id)
            return None
        response.raise_for_status()
        return response.json()
