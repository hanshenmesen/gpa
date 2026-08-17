import unittest
from unittest.mock import patch

import gpa.llm as llm


class FakeImage:
    def save(self, buffer, format=None):
        buffer.write(b"fake-png")


class FakeChoice:
    message = type("Message", (), {"content": '{"ok": true}'})()


class FakeResponse:
    choices = [FakeChoice()]
    usage = type(
        "Usage",
        (),
        {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "prompt_tokens_details": type("PromptDetails", (), {"cached_tokens": 20})(),
            "completion_tokens_details": type("CompletionDetails", (), {"reasoning_tokens": 5})(),
        },
    )()


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
    def test_default_client_uses_bounded_timeout_and_retries(self):
        old_client = llm._client
        old_key = llm.LLM_API_KEY
        old_timeout = llm.LLM_REQUEST_TIMEOUT_SECONDS
        old_retries = llm.LLM_CLIENT_MAX_RETRIES
        llm._client = None
        llm.LLM_API_KEY = "test-key"
        llm.LLM_REQUEST_TIMEOUT_SECONDS = 23.0
        llm.LLM_CLIENT_MAX_RETRIES = 1
        try:
            with patch("openai.OpenAI", return_value=object()) as openai_client:
                llm._get_client()
            openai_client.assert_called_once_with(
                api_key="test-key",
                base_url=llm.LLM_BASE_URL,
                timeout=23.0,
                max_retries=1,
            )
        finally:
            llm._client = old_client
            llm.LLM_API_KEY = old_key
            llm.LLM_REQUEST_TIMEOUT_SECONDS = old_timeout
            llm.LLM_CLIENT_MAX_RETRIES = old_retries

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
        old_vision_model = llm.LLM_VISION_MODEL
        try:
            llm._get_client = lambda: client
            llm.LLM_MODEL = "gpt-5.5"
            llm.LLM_VISION_MODEL = ""

            result = llm.call_json_llm("system", "user", image=FakeImage(), attempts=1)
        finally:
            llm._get_client = old_get_client
            llm.LLM_MODEL = old_model
            llm.LLM_VISION_MODEL = old_vision_model

        self.assertEqual(result, {"ok": True})
        self.assertNotIn("temperature", client.chat.completions.kwargs)

    def test_text_and_vision_model_overrides_route_by_input(self):
        client = FakeClient()
        old_get_client = llm._get_client
        old_model = llm.LLM_MODEL
        old_text_model = llm.LLM_TEXT_MODEL
        old_vision_model = llm.LLM_VISION_MODEL
        try:
            llm._get_client = lambda: client
            llm.LLM_MODEL = "fallback-model"
            llm.LLM_TEXT_MODEL = "text-model"
            llm.LLM_VISION_MODEL = "vision-model"

            llm.call_json_llm("system", "text", attempts=1)
            text_model = client.chat.completions.kwargs["model"]
            llm.call_json_llm("system", "vision", image=FakeImage(), attempts=1)
            vision_model = client.chat.completions.kwargs["model"]
        finally:
            llm._get_client = old_get_client
            llm.LLM_MODEL = old_model
            llm.LLM_TEXT_MODEL = old_text_model
            llm.LLM_VISION_MODEL = old_vision_model

        self.assertEqual(text_model, "text-model")
        self.assertEqual(vision_model, "vision-model")

    def test_empty_model_overrides_preserve_legacy_model(self):
        old_model = llm.LLM_MODEL
        old_text_model = llm.LLM_TEXT_MODEL
        old_vision_model = llm.LLM_VISION_MODEL
        try:
            llm.LLM_MODEL = "legacy-model"
            llm.LLM_TEXT_MODEL = ""
            llm.LLM_VISION_MODEL = ""
            self.assertEqual(llm._configured_model(), "legacy-model")
            self.assertEqual(llm._configured_model(image=FakeImage()), "legacy-model")
        finally:
            llm.LLM_MODEL = old_model
            llm.LLM_TEXT_MODEL = old_text_model
            llm.LLM_VISION_MODEL = old_vision_model

    def test_custom_provider_uses_shared_retry_contract(self):
        class Provider:
            def __init__(self):
                self.calls = 0

            def call_json(self, system_prompt, user_prompt, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient")
                return {"provider": "custom", "prompt": user_prompt}

        provider = Provider()
        try:
            llm.set_llm_provider(provider)
            result = llm.call_json_llm(
                "system",
                "portable request",
                attempts=2,
                retry_sleep=0,
            )
        finally:
            llm.set_llm_provider(None)

        self.assertEqual(provider.calls, 2)
        self.assertEqual(result, {"provider": "custom", "prompt": "portable request"})

    def test_default_provider_routes_failed_retry_to_fallback_model(self):
        class FlakyCompletions:
            def __init__(self):
                self.models = []

            def create(self, **kwargs):
                self.models.append(kwargs["model"])
                if len(self.models) == 1:
                    raise RuntimeError("primary unavailable")
                return FakeResponse()

        client = FakeClient()
        client.chat.completions = FlakyCompletions()
        old_get_client = llm._get_client
        old_text_model = llm.LLM_TEXT_MODEL
        old_text_fallback = llm.LLM_TEXT_FALLBACK_MODEL
        try:
            llm._get_client = lambda: client
            llm.LLM_TEXT_MODEL = "text-primary"
            llm.LLM_TEXT_FALLBACK_MODEL = "text-fallback"
            result = llm.call_json_llm(
                "system", "portable request", attempts=2, retry_sleep=0
            )
        finally:
            llm._get_client = old_get_client
            llm.LLM_TEXT_MODEL = old_text_model
            llm.LLM_TEXT_FALLBACK_MODEL = old_text_fallback

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.chat.completions.models, ["text-primary", "text-fallback"])

    def test_call_metrics_capture_model_modality_latency_and_usage(self):
        client = FakeClient()
        old_get_client = llm._get_client
        old_model = llm.LLM_MODEL
        old_vision_model = llm.LLM_VISION_MODEL
        try:
            llm.clear_llm_metrics()
            llm._get_client = lambda: client
            llm.LLM_MODEL = "fallback"
            llm.LLM_VISION_MODEL = "vision-cost-model"
            llm.call_json_llm("system", "user", image=FakeImage(), attempts=1)
            metrics = llm.consume_llm_metrics()
        finally:
            llm._get_client = old_get_client
            llm.LLM_MODEL = old_model
            llm.LLM_VISION_MODEL = old_vision_model

        self.assertEqual(len(metrics), 1)
        metric = metrics[0]
        self.assertEqual(metric["model"], "vision-cost-model")
        self.assertEqual(metric["modality"], "vision")
        self.assertEqual(metric["prompt_tokens"], 120)
        self.assertEqual(metric["completion_tokens"], 30)
        self.assertEqual(metric["total_tokens"], 150)
        self.assertEqual(metric["cached_tokens"], 20)
        self.assertEqual(metric["reasoning_tokens"], 5)
        self.assertGreaterEqual(metric["duration_ms"], 0)


if __name__ == "__main__":
    unittest.main()
