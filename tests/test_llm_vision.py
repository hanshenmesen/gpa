import unittest

import gpa.llm as llm


class FakeImage:
    def save(self, buffer, format=None):
        buffer.write(b"fake-png")


class FakeChoice:
    message = type("Message", (), {"content": '{"ok": true}'})()


class FakeResponse:
    choices = [FakeChoice()]


class FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return FakeResponse()


class FakeChat:
    def __init__(self):
        self.completions = FakeCompletions()


class FakeClient:
    def __init__(self):
        self.chat = FakeChat()


class LLMVisionTests(unittest.TestCase):
    def test_call_json_llm_sends_image_url_content(self):
        client = FakeClient()
        old_get_client = llm._get_client
        old_model = llm.LLM_MODEL
        try:
            llm._get_client = lambda: client
            llm.LLM_MODEL = "gpt-4.1-mini"

            result = llm.call_json_llm("system", "user", image=FakeImage(), attempts=1)
        finally:
            llm._get_client = old_get_client
            llm.LLM_MODEL = old_model

        self.assertEqual(result, {"ok": True})
        messages = client.chat.completions.kwargs["messages"]
        content = messages[1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "user"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(content[1]["image_url"]["detail"], "high")
        self.assertIn("temperature", client.chat.completions.kwargs)

    def test_call_json_llm_omits_temperature_for_gpt5_family(self):
        client = FakeClient()
        old_get_client = llm._get_client
        old_model = llm.LLM_MODEL
        try:
            llm._get_client = lambda: client
            llm.LLM_MODEL = "gpt-5.5"

            result = llm.call_json_llm("system", "user", image=FakeImage(), attempts=1)
        finally:
            llm._get_client = old_get_client
            llm.LLM_MODEL = old_model

        self.assertEqual(result, {"ok": True})
        self.assertNotIn("temperature", client.chat.completions.kwargs)


if __name__ == "__main__":
    unittest.main()
