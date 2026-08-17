import unittest

from gpa.cloud.service_config import (
    CloudServiceConfig,
    CloudServiceConfigurationError,
)


class CloudServiceConfigTests(unittest.TestCase):
    def test_cloud_is_offline_safe_until_api_is_configured(self):
        config = CloudServiceConfig.from_environment({})
        self.assertFalse(config.enabled)
        self.assertTrue(config.web_base_url.startswith("https://"))
        with self.assertRaisesRegex(CloudServiceConfigurationError, "not configured"):
            config.api_url("/v1/me")

    def test_production_endpoints_use_https_and_wss(self):
        config = CloudServiceConfig.from_environment(
            {
                "GPA_CLOUD_ENV": "production",
                "GPA_CLOUD_API_URL": "https://api.gpa.example/",
                "GPA_CLOUD_WEB_URL": "https://app.gpa.example/",
            }
        )
        self.assertTrue(config.enabled)
        self.assertEqual(config.api_url("/v1/health"), "https://api.gpa.example/v1/health")
        self.assertEqual(
            config.agent_gateway_url,
            "wss://api.gpa.example/v1/agent/connect",
        )

    def test_loopback_http_is_allowed_only_for_development(self):
        config = CloudServiceConfig.from_environment(
            {
                "GPA_CLOUD_API_URL": "http://127.0.0.1:8080",
                "GPA_CLOUD_WEB_URL": "http://localhost:3000",
            }
        )
        self.assertEqual(config.agent_gateway_url, "ws://127.0.0.1:8080/v1/agent/connect")
        with self.assertRaisesRegex(CloudServiceConfigurationError, "Production"):
            CloudServiceConfig.from_environment(
                {
                    "GPA_CLOUD_ENV": "production",
                    "GPA_CLOUD_API_URL": "http://localhost:8080",
                }
            )

    def test_remote_http_and_embedded_credentials_are_rejected(self):
        for api_url in (
            "http://api.gpa.example",
            "https://user:secret@api.gpa.example",
            "https://api.gpa.example/#token",
        ):
            with self.subTest(api_url=api_url), self.assertRaises(
                CloudServiceConfigurationError
            ):
                CloudServiceConfig.from_environment({"GPA_CLOUD_API_URL": api_url})


if __name__ == "__main__":
    unittest.main()
