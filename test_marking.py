"""Checks for automatic marking. Run with:  python3 test_marking.py"""

import unittest

from quiz_server import _to_number, is_correct, safe_cell


class Accepted(unittest.TestCase):
    CASES = [
        # (student types, answer in the quiz file)
        ("4", "4"), ("x = 4", "4"), ("4.0", "4"), (".5", "0.5"), ("1/2", "0.5"),
        ("−3", "-3"), ("- 3", "-3"),
        ("1 1/2", "1.5"), ("1 1/2", "3/2"), ("-2 1/4", "-2.25"),
        ("1 000", "1000"), ("1,000", "1000"),
        ("5 cm", "5"), ("5cm", "5"), ("5 m", "5"), ("45°", "45"), ("45 degrees", "45°"),
        ("50%", "50"), ("50", "50%"), ("$12", "12"), ("12 cm²", "12 cm^2"), ("12", "12 cm²"),
        ("2b + 3a", "3a + 2b"), ("3a+2b", "3a + 2b"), ("-2b + 5a", "5a - 2b"),
        ("3ba", "3ab"),("2 × a", "2a"), ("a*2", "2a"),
        ("x^2 + 3x", "3x + x²"), ("x**2", "x²"), ("(x + 3)(x + 2)", "(x+2)(x+3)"),
        ("(2 + x)(x + 3)", "(x+2)(x+3)"), ("2(3 + x)", "2(x + 3)"),
        ("2sqrt3", "2√3"), ("sqrt(12)", "√12"), ("2pi", "2π"), ("y = 3 + 2x", "2x + 3"),
        ("ISOSCELES", "isosceles"), ("Isosceles.", "isosceles"),
    ]

    def test_accepted(self):
        for student, answer in self.CASES:
            with self.subTest(student=student, answer=answer):
                self.assertTrue(is_correct(student, [answer]))

    def test_any_listed_answer(self):
        self.assertTrue(is_correct("1/2", ["0.25", "1/2"]))
        self.assertFalse(is_correct("1/3", ["0.25", "1/2"]))


class Rejected(unittest.TestCase):
    CASES = [
        ("", "4"), ("5", "4"), ("11/2", "1 1/2"), ("11/2", "1.5"), ("2,3", "23"),
        ("5 m", "5 cm"), ("500 cm", "5 m"), ("0.5", "50%"), ("50%", "0.5"),
        ("5", "5m"),              # algebra: 2m + 3m = 5m, so "5" is wrong
        ("a + a", "2a"),          # not simplified
        ("2(x + 3)", "2x + 6"),   # not expanded
        ("2x + 6", "2(x + 3)"),   # not factorised
        ("3 × 4", "12"),          # working, not the answer
        ("x2", "2x"),             # unclear, don't guess
        ("3a + 2c", "3a + 2b"), ("3a - 2b", "3a + 2b"), ("x^3", "x^2"),
        ("silent", "listen"),     # words are text, not letters multiplied
        ("x > 3", "x < 3"),
    ]

    def test_rejected(self):
        for student, answer in self.CASES:
            with self.subTest(student=student, answer=answer):
                self.assertFalse(is_correct(student, [answer]))


class Spreadsheet(unittest.TestCase):
    def test_negative_numbers_stay_numbers(self):
        self.assertEqual(safe_cell("-3"), "-3")
        self.assertEqual(safe_cell("-2 1/4"), "-2 1/4")

    def test_formulas_are_neutralised(self):
        self.assertEqual(safe_cell("=1+1"), "'=1+1")
        self.assertEqual(safe_cell("-cmd"), "'-cmd")

    def test_to_number(self):
        self.assertEqual(_to_number("1 1/2"), _to_number("3/2"))
        self.assertIsNone(_to_number("1/0"))


if __name__ == "__main__":
    unittest.main()
