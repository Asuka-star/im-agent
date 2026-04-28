import unittest

from app.services.session_display_service import SessionDisplayService


class _StubMemoryService:
    def __init__(self, aliases: dict[tuple[str, str], str] | None = None) -> None:
        self.aliases = aliases or {}

    def get_alias_display_name(self, session_id: str, identifier: str | None) -> str | None:
        key = (session_id, identifier or "")
        return self.aliases.get(key)


class _StubUserAPI:
    def __init__(self, responses: dict[tuple[str, str], str] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[dict] = []

    def get_user_display_name(
        self,
        *,
        user_id: str | None = None,
        open_id: str | None = None,
    ) -> str | None:
        self.calls.append({"user_id": user_id, "open_id": open_id})
        if user_id:
            return self.responses.get(("user_id", user_id))
        if open_id:
            return self.responses.get(("open_id", open_id))
        return None


class _StubChatAPI:
    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[str] = []

    def get_chat_name(self, chat_id: str) -> str | None:
        self.calls.append(chat_id)
        return self.responses.get(chat_id)


class SessionDisplayServiceTests(unittest.TestCase):
    def test_group_chat_prefers_chat_name_and_caches(self) -> None:
        chat_api = _StubChatAPI({"oc_group_demo": "AI 协作群"})
        service = SessionDisplayService(
            memory_service=_StubMemoryService(),
            user_api=_StubUserAPI(),
            chat_api=chat_api,
        )

        first = service.resolve_session_label(
            session_id="oc_group_demo",
            source_type="group",
            source_ref="oc_group_demo",
            created_by="ou_user_1",
        )
        second = service.resolve_session_label(
            session_id="oc_group_demo",
            source_type="group",
            source_ref="oc_group_demo",
            created_by="ou_user_1",
        )

        self.assertEqual(first, "AI 协作群")
        self.assertEqual(second, "AI 协作群")
        self.assertEqual(chat_api.calls, ["oc_group_demo"])

    def test_p2p_falls_back_to_cached_alias_when_user_lookup_misses(self) -> None:
        user_api = _StubUserAPI()
        service = SessionDisplayService(
            memory_service=_StubMemoryService({("oc_p2p_demo", "u_demo"): "产品经理张三"}),
            user_api=user_api,
            chat_api=_StubChatAPI(),
        )

        label = service.resolve_session_label(
            session_id="oc_p2p_demo",
            source_type="p2p",
            source_ref="oc_p2p_demo",
            created_by="u_demo",
        )

        self.assertEqual(label, "产品经理张三")
        self.assertEqual(
            user_api.calls,
            [
                {"user_id": "u_demo", "open_id": None},
                {"user_id": None, "open_id": "u_demo"},
            ],
        )

    def test_p2p_falls_back_to_user_profile_and_caches(self) -> None:
        user_api = _StubUserAPI({("open_id", "ou_demo"): "李四"})
        service = SessionDisplayService(
            memory_service=_StubMemoryService(),
            user_api=user_api,
            chat_api=_StubChatAPI(),
        )

        first = service.resolve_session_label(
            session_id="oc_p2p_demo",
            source_type="p2p",
            source_ref="oc_p2p_demo",
            created_by="ou_demo",
        )
        second = service.resolve_session_label(
            session_id="oc_p2p_demo",
            source_type="p2p",
            source_ref="oc_p2p_demo",
            created_by="ou_demo",
        )

        self.assertEqual(first, "李四")
        self.assertEqual(second, "李四")
        self.assertEqual(
            user_api.calls,
            [
                {"user_id": "ou_demo", "open_id": None},
                {"user_id": None, "open_id": "ou_demo"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
