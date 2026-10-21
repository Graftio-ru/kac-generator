"""Letter extraction must accept the JSON object required by DeepSeek."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from engine.llm import LLMUnavailable, ModelOutputInvalid
from engine.parsers.letters import parse_letter


class LetterResponseTests(unittest.TestCase):
    def _parse(self, response):
        doc = MagicMock()
        doc.page_count = 1
        doc.__getitem__.return_value.get_text.return_value = "Счёт: кабель 100 руб."
        with patch("engine.parsers.letters.fitz.open", return_value=doc), \
             patch("engine.parsers.letters.complete_json", return_value=response):
            return parse_letter("test.pdf")

    def test_accepts_deepseek_object(self):
        items = [{"name": "Кабель", "price_with_vat": 100}]
        self.assertEqual(items, self._parse({"items": items}))

    def test_preserves_legacy_array(self):
        items = [{"name": "Кабель", "price_with_vat": 100}]
        self.assertEqual(items, self._parse(items))

    def test_rejects_malformed_object(self):
        with self.assertRaises(ModelOutputInvalid):
            self._parse({"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
