from __future__ import annotations

import time
from typing import Any

import requests


class VaultSpeedApiError(RuntimeError):
    pass


class VaultSpeedClient:
    def __init__(self, base_url: str, access_token: str, environment: str = "", timeout_seconds: int = 120):
        self.environment = environment.strip()
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {access_token}"})

    @staticmethod
    def login(base_url: str, username: str, password: str) -> str:
        url = base_url.rstrip("/") + "/login"
        response = requests.post(
            url,
            json={"username": username, "password": password},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise VaultSpeedApiError("Login succeeded but no access_token was returned.")
        return token

    def list_projects(self) -> list[dict[str, Any]]:
        return self._ensure_list(self._get_json("projects"), "projects")

    def list_data_vaults(self, project_name: str) -> list[dict[str, Any]]:
        return self._ensure_list(
            self._get_json("data-vaults", params={"filter[where][project_name]": project_name}),
            "data vaults",
        )

    def list_data_vault_releases(self, dvid: int) -> list[dict[str, Any]]:
        return self._ensure_list(self._get_json(f"data-vault/{dvid}/releases"), "data vault releases")

    def list_business_vault_releases(self, dvid: int, dvvsid: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"filter[where][dv_id]": dvid}
        if dvvsid is not None:
            params["filter[where][dv_vs_id]"] = dvvsid
        return self._ensure_list(
            self._get_json(f"data-vaults/{dvid}/business-vaults/releases", params=params),
            "business vault releases",
        )

    def list_metadata_export_presets(self) -> list[dict[str, Any]]:
        return self._ensure_list(self._get_json("metadata-export/presets"), "metadata export presets")

    def export_metadata(self, preset_id: int, bv_vs_id: int) -> str:
        """Trigger export, poll until ready, download and return CSV content."""
        self._start_export(preset_id=preset_id, bvid=bv_vs_id)
        self._wait_until_ready()
        return self._download_export(preset_id=preset_id)

    def _start_export(self, preset_id: int, bvid: int) -> int:
        response = self.session.post(
            f"{self.base_url}metadata-export/preset/{preset_id}/export",
            json={"bv_vs_id": bvid},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        task_set_id = payload.get("task_set_id")
        if task_set_id is None:
            raise VaultSpeedApiError("Export task started but task_set_id was not returned.")
        return int(task_set_id)

    def _wait_until_ready(self) -> None:
        while True:
            payload = self._get_json("metadata-export/preset/check")
            if bool(payload.get("ready")):
                return
            time.sleep(5)

    def _download_export(self, preset_id: int) -> str:
        response = self.session.get(
            f"{self.base_url}metadata-export/preset/{preset_id}/download",
            timeout=60,
        )
        response.raise_for_status()
        return response.text

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _ensure_list(self, payload: Any, item_name: str) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise VaultSpeedApiError(f"Unexpected {item_name} response payload.")
        return [item for item in payload if isinstance(item, dict)]
