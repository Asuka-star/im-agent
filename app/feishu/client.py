import httpx


class FeishuClient:
    def __init__(self, base_url: str = "https://open.feishu.cn") -> None:
        self.base_url = base_url
        self.client = httpx.Client(base_url=base_url, timeout=20.0)

    def close(self) -> None:
        self.client.close()
