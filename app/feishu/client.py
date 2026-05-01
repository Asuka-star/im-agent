import httpx


class FeishuClient:
    def __init__(self, base_url: str = "https://open.feishu.cn") -> None:
        self.base_url = base_url
        self.client = httpx.Client(base_url=base_url, timeout=20.0)

    def post_json(
        self,
        path: str,
        *,
        json: dict,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        response = self.client.post(path, json=json, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("code", 0) != 0:
            raise RuntimeError(f"Feishu API error: {data.get('msg', 'unknown error')}")
        return data

    def get_json(
        self,
        path: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        response = self.client.get(path, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("code", 0) != 0:
            raise RuntimeError(f"Feishu API error: {data.get('msg', 'unknown error')}")
        return data

    def delete_json(
        self,
        path: str,
        *,
        json: dict,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        response = self.client.request("DELETE", path, json=json, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("code", 0) != 0:
            raise RuntimeError(f"Feishu API error: {data.get('msg', 'unknown error')}")
        return data

    def patch_json(
        self,
        path: str,
        *,
        json: dict,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        response = self.client.patch(path, json=json, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("code", 0) != 0:
            raise RuntimeError(f"Feishu API error: {data.get('msg', 'unknown error')}")
        return data

    def get_response(
        self,
        path: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        response = self.client.get(path, headers=headers, params=params)
        response.raise_for_status()
        return response

    def close(self) -> None:
        self.client.close()
