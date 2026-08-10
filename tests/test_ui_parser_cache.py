import unittest

import numpy as np
from PIL import Image

import gpa.core.ui_parser as ui_parser


class UIParserCacheTests(unittest.TestCase):
    def setUp(self):
        ui_parser.clear_parse_cache()
        self._old_cache_size = ui_parser.UI_PARSE_CACHE_SIZE
        ui_parser.UI_PARSE_CACHE_SIZE = 4

        self._old_detect_icons = ui_parser._detect_icons
        self._old_detect_text = ui_parser._detect_text
        self._old_icon_embeddings = ui_parser._compute_icon_embeddings
        self._old_text_embeddings = ui_parser._compute_text_embeddings

    def tearDown(self):
        ui_parser.clear_parse_cache()
        ui_parser.UI_PARSE_CACHE_SIZE = self._old_cache_size
        ui_parser._detect_icons = self._old_detect_icons
        ui_parser._detect_text = self._old_detect_text
        ui_parser._compute_icon_embeddings = self._old_icon_embeddings
        ui_parser._compute_text_embeddings = self._old_text_embeddings

    def test_parse_screenshot_reuses_identical_image_graph(self):
        calls = {"icons": 0, "text": 0}

        def fake_detect_icons(image):
            calls["icons"] += 1
            return [{"pos": [10.0, 10.0, 20.0, 20.0], "conf": 0.9}]

        def fake_detect_text(image):
            calls["text"] += 1
            return [{"pos": [40.0, 10.0, 40.0, 20.0], "content": "OK", "conf": 0.9}]

        ui_parser._detect_icons = fake_detect_icons
        ui_parser._detect_text = fake_detect_text
        ui_parser._compute_icon_embeddings = lambda image, boxes: np.ones((len(boxes), 512), dtype=np.float32)
        ui_parser._compute_text_embeddings = lambda texts: np.ones((len(texts), 384), dtype=np.float32)

        image = Image.new("RGB", (100, 80), color="white")
        first = ui_parser.parse_screenshot(image)
        first.nodes[0].content = "mutated"

        second = ui_parser.parse_screenshot(image)

        self.assertEqual(calls, {"icons": 1, "text": 1})
        self.assertEqual(len(second.nodes), 2)
        self.assertEqual(second.nodes[1].content, "OK")


if __name__ == "__main__":
    unittest.main()
