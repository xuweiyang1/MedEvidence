"""Broad-question detection: generic questions get a scope hint, specific ones do not."""
from __future__ import annotations
import unittest

from agent.scope import detect_broad_question


class TestBroadScope(unittest.TestCase):
    def test_chinese_generic_question_is_broad(self):
        scope = detect_broad_question("心脏病常见的治疗方式有哪些？")
        self.assertIsNotNone(scope)
        self.assertIn("宽泛", scope.message)
        self.assertTrue(scope.suggestions)

    def test_specific_condition_is_not_broad(self):
        self.assertIsNone(detect_broad_question("高血压的常见治疗方法有哪些？"))
        self.assertIsNone(detect_broad_question("心力衰竭的一线药物治疗有哪些？"))

    def test_english_generic_question_is_broad(self):
        self.assertIsNotNone(detect_broad_question("common treatment options for heart disease"))

    def test_english_specific_question_is_not_broad(self):
        self.assertIsNone(detect_broad_question("SGLT2 inhibitors for heart failure treatment"))

    def test_empty_question_is_not_broad(self):
        self.assertIsNone(detect_broad_question("   "))


if __name__ == "__main__":
    unittest.main()
