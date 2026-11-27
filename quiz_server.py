#!/usr/bin/env python3
"""
Classroom Quiz Server
=====================

A small quiz website that runs on YOUR computer. Students on the same school
network open it in their browser, answer the questions, and their answers are
saved to a spreadsheet (CSV) file in the "results" folder on your computer.

Nothing is sent to the internet, to Claude, or to any other service.

Run it with:   python quiz_server.py
Then open:     http://localhost:8000/teacher   (teacher view, this computer only)
Students use the address printed when the server starts.

Only needs Python 3.8+ (no extra packages).
"""

import argparse
import csv
import glob
import hashlib
import html
import json
import os
import random
import re
import platform
import secrets
import socket
import sys
import threading
from collections import OrderedDict
from datetime import datetime
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUIZ_DIR = os.path.join(BASE_DIR, "quizzes")
RESULTS_DIR = os.path.join(BASE_DIR, "results")   # may be changed at start-up, see choose_results_dir()
RESULTS_WHERE = "on this computer"
SETTINGS_FILE = os.path.join(BASE_DIR, "quiz-settings.txt")
ONEDRIVE_FOLDER = "Quiz results"   # make this folder in your school OneDrive to save results there
CLASS_DIR = os.path.join(BASE_DIR, "classes")            # optional class lists
CLOSED_FILE = os.path.join(BASE_DIR, "closed-quizzes.txt")

MAX_BODY = 200_000       # bytes accepted in one submission
MAX_ANSWER_LEN = 1000    # characters kept per answer
MAX_NAME_LEN = 80
MAX_RECENT = 5000        # finished submissions remembered (to stop duplicates)

write_lock = threading.Lock()


# --------------------------------------------------------------------------
# Quiz file format
# --------------------------------------------------------------------------
#
#   title: Solving Linear Equations
#   instructions: Show your working on paper. Type only the final answer.
#   show_answers: yes
#
#   Q: Solve 2x + 3 = 11
#   A: 4
#
#   Q: Write 3/4 as a decimal.
#   A: 0.75 | .75
#
#   Q: Which value of x makes x² = 9 true?
#   - 2
#   * 3
#   - 4.5
#
#   Q: Explain in one sentence what "solve" means.
#   (no A: line and no options = written answer, marked by you)
#
# --------------------------------------------------------------------------

def parse_quiz(text):
    quiz = {"title": "Quiz", "instructions": "", "show_answers": True, "shuffle": "no", "questions": []}
    current = None

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        m = re.match(r"^(Q|A)\s*[:.]\s*(.*)$", stripped, re.IGNORECASE)
        if m and m.group(1).upper() == "Q":
            current = {"text": m.group(2), "answers": [], "options": [], "correct_option": None}
            quiz["questions"].append(current)
            continue

        if current is None:
            km = re.match(r"^([a-z_]+)\s*:\s*(.*)$", stripped, re.IGNORECASE)
            if km:
                key, value = km.group(1).lower(), km.group(2).strip()
                if key == "title":
                    quiz["title"] = value
                elif key == "instructions":
                    quiz["instructions"] = value
                elif key == "show_answers":
                    quiz["show_answers"] = value.lower() in ("yes", "y", "true", "1")
                elif key == "shuffle":
                    v = value.lower()
                    quiz["shuffle"] = ("both" if v in ("yes", "y", "true", "1", "both")
                                       else v if v in ("questions", "options") else "no")
            continue

        if m and m.group(1).upper() == "A":
            current["answers"] = [a.strip() for a in m.group(2).split("|") if a.strip()]
        elif stripped[:2] in ("- ", "* "):
            if stripped[0] == "*":
                current["correct_option"] = len(current["options"])
            current["options"].append(stripped[2:].strip())
        else:
            # continuation of the question text
            current["text"] += "\n" + stripped

    for q in quiz["questions"]:
        if q["options"]:
            q["kind"] = "choice"
        elif q["answers"]:
            q["kind"] = "short"
        else:
            q["kind"] = "written"
    return quiz


def quiz_version(quiz):
    """Changes when questions are added, removed, reordered or reworded (not when an answer is fixed)."""
    shape = [(q["kind"], q["text"], q["options"]) for q in quiz["questions"]]
    return hashlib.sha1(repr(shape).encode("utf-8")).hexdigest()[:12]


def list_quizzes():
    quizzes = {}
    if not os.path.isdir(QUIZ_DIR):
        return quizzes
    closed = closed_ids()
    for name in sorted(os.listdir(QUIZ_DIR)):
        if not name.lower().endswith(".txt"):
            continue
        quiz_id = name[:-4]
        try:
            with open(os.path.join(QUIZ_DIR, name), encoding="utf-8-sig") as f:
                quiz = parse_quiz(f.read())
        except (OSError, UnicodeDecodeError):
            continue
        if quiz["questions"]:
            quiz["id"] = quiz_id
            quiz["open"] = quiz_id not in closed
            quiz["version"] = quiz_version(quiz)
            quizzes[quiz_id] = quiz
    return quizzes


def closed_ids():
    try:
        with open(CLOSED_FILE, encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except OSError:
        return set()


def set_open(quiz_id, is_open):
    with write_lock:
        ids = closed_ids()
        (ids.discard if is_open else ids.add)(quiz_id)
        with open(CLOSED_FILE, "w", encoding="utf-8") as f:
            f.writelines(f"{i}\n" for i in sorted(ids))


def read_rosters():
    """classes/9SD.txt -> {"9SD": ["Aroha Smith", ...]}. One name per line."""
    rosters = {}
    if not os.path.isdir(CLASS_DIR):
        return rosters
    for name in sorted(os.listdir(CLASS_DIR), key=str.lower):
        if not name.lower().endswith(".txt"):
            continue
        try:
            with open(os.path.join(CLASS_DIR, name), encoding="utf-8-sig") as f:
                names = {" ".join(line.split())[:MAX_NAME_LEN] for line in f}
        except (OSError, UnicodeDecodeError):
            continue
        names = sorted((n for n in names if n and not n.startswith("#")), key=str.lower)
        if names:
            rosters[name[:-4][:40]] = names
    return rosters


# --------------------------------------------------------------------------
# Marking
# --------------------------------------------------------------------------

def _to_number(s):
    """Read a plain number: 4, -0.5, .5, 3/4, 1 1/2 (mixed), 1 000 or 1,000. Else None."""
    s = s.strip().replace("−", "-").replace("–", "-")
    try:
        # thousands separators: "1 000", "12,500.5"
        if re.fullmatch(r"[-+]?\d{1,3}([ ,]\d{3})+(\.\d+)?", s):
            return Fraction(re.sub(r"[ ,]", "", s))
        s = re.sub(r"(?<=[-+])\s+", "", s)            # "- 3" -> "-3"
        m = re.fullmatch(r"([-+]?)(\d+)\s+(\d+)/(\d+)", s)   # mixed number "1 1/2"
        if m:
            value = int(m.group(2)) + Fraction(int(m.group(3)), int(m.group(4)))
            return -value if m.group(1) == "-" else value
        if re.fullmatch(r"[-+]?\d+\s*/\s*\d+", s):
            return Fraction(s.replace(" ", ""))
        if re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)", s):
            return Fraction(s)
    except (ValueError, ZeroDivisionError):
        return None
    return None


# Units a student may add after a number. Single letters (m, g, s ...) are only
# treated as units in a student's answer when the accepted answer is a plain
# number, because in algebra "5m" means 5 × m.
_UNIT_ALIASES = {
    "mm": "mm", "cm": "cm", "km": "km", "mg": "mg", "kg": "kg", "ml": "ml",
    "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "min": "min", "mins": "min", "minute": "min", "minutes": "min",
    "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "°": "°", "degree": "°", "degrees": "°", "%": "%",
    "unit": "units", "units": "units", "$": "$",
}
_SINGLE_LETTER_UNITS = {"m": "m", "g": "g", "l": "l", "s": "s", "h": "h"}


def _number_and_unit(s, single_letter_units):
    """Split "5 cm²" into (Fraction(5), "cm^2"). Returns (None, "") if not a number."""
    s = s.strip()
    unit = ""
    if s.startswith("$"):
        s, unit = s[1:].strip(), "$"
    else:
        m = re.fullmatch(r"(.*?)\s*([a-z]+|°|%)(\^[23])?", s)
        if m:
            name = m.group(2)
            canon = _UNIT_ALIASES.get(name) or (single_letter_units and _SINGLE_LETTER_UNITS.get(name))
            if canon:
                s, unit = m.group(1), canon + (m.group(3) or "")
    return _to_number(s), unit


def _normalise(s):
    s = s.strip().lower()
    for old, new in (("−", "-"), ("–", "-"), ("×", "*"), ("·", "*"), ("÷", "/"),
                     ("**", "^"), ("²", "^2"), ("³", "^3"), ("sqrt", "√")):
        s = s.replace(old, new)
    s = re.sub(r"^[a-z]\s*=\s*", "", s)   # "x = 4" -> "4"
    s = s.rstrip(".")
    return re.sub(r"\s+", " ", s).strip()


# ---- Algebra: accept the same terms written in a different order ----------
# "3a + 2b" == "2b + 3a", "(x+2)(x+3)" == "(x+3)(x+2)", "ab" == "ba".
# It does NOT simplify or expand, so "a + a" is not accepted for "2a", and
# "2(x + 3)" is not accepted for "2x + 6". Anything it can't read is compared
# as plain text instead.

class _NotAlgebra(Exception):
    pass


class _ExprReader:
    _NUMBER = re.compile(r"\d+\.?\d*|\.\d+")

    def __init__(self, text):
        self.s, self.i = text, 0

    def peek(self):
        return self.s[self.i] if self.i < len(self.s) else ""

    def read_all(self):
        key = self.expr()
        if self.i != len(self.s):
            raise _NotAlgebra
        return key

    def expr(self):
        terms, sign = [], "+"
        while True:
            while self.peek() in ("+", "-") and self.peek():
                if self.s[self.i] == "-":
                    sign = "-" if sign == "+" else "+"
                self.i += 1
            terms.append((sign,) + self.term())
            if not (self.peek() and self.peek() in "+-"):
                return ("e", tuple(sorted(terms, key=repr)))
            sign = "+"

    def term(self):
        top, bottom = [self.factor(number_ok=True)], []
        after_divide = False
        while True:
            c = self.peek()
            if c == "*":
                self.i += 1
                top.append(self.factor(number_ok=True))
                after_divide = False
            elif c == "/":
                self.i += 1
                bottom.append(self.factor(number_ok=True))
                after_divide = True
            elif c and (c.isalpha() or c in "(√"):
                if after_divide:          # "x/2y" is ambiguous
                    raise _NotAlgebra
                top.append(self.factor(number_ok=False))
            else:
                return tuple(sorted(top, key=repr)), tuple(sorted(bottom, key=repr))

    def factor(self, number_ok):
        base = self.atom(number_ok)
        if self.peek() == "^":
            self.i += 1
            negative = self.peek() == "-"
            if negative:
                self.i += 1
            power = self.atom(True)
            base = ("p", base, ("-", power) if negative else power)
        return base

    def atom(self, number_ok):
        c = self.peek()
        m = self._NUMBER.match(self.s, self.i)
        if m:
            if not number_ok:             # "x2" or "(x+1)2": unclear, don't guess
                raise _NotAlgebra
            self.i = m.end()
            return ("n", Fraction(m.group()))
        if c and c.isalpha():
            self.i += 1
            return ("v", c)
        if c == "√":
            self.i += 1
            return ("r", self.atom(True))
        if c == "(":
            self.i += 1
            inner = self.expr()
            if self.peek() != ")":
                raise _NotAlgebra
            self.i += 1
            terms = inner[1]
            if len(terms) == 1 and terms[0][0] == "+" and len(terms[0][1]) == 1 and not terms[0][2]:
                return terms[0][1][0]         # "(x)" -> x, "√(12)" -> √12
            return ("b", inner)
        raise _NotAlgebra


def _expression_key(s):
    s = s.replace(" ", "").replace("pi", "π")
    if s.count("=") == 1:
        left, right = s.split("=")
        return ("=", _ExprReader(left).read_all(), _ExprReader(right).read_all())
    return _ExprReader(s).read_all()


def _same_answer(s, a):
    """s and a are already normalised."""
    a_num, a_unit = _number_and_unit(a, single_letter_units=False)
    if a_num is not None:
        # Compare numbers by value only: as text, "11/2" would match "1 1/2".
        s_num, s_unit = _number_and_unit(s, single_letter_units=(a_unit == ""))
        if s_num is None or s_num != a_num:
            return False
        # Any unit is fine when the answer has none; otherwise it must match (or be left off).
        return a_unit == "" or s_unit in ("", a_unit)

    if s.replace(" ", "") == a.replace(" ", ""):
        return True

    # Plain words ("isosceles") are only compared as text, so that anagrams
    # don't count as a reordered product of letters.
    if not re.search(r"[^a-z ]", a):
        return False
    try:
        return _expression_key(s) == _expression_key(a)
    except (_NotAlgebra, ValueError, ZeroDivisionError):
        return False


def is_correct(student, accepted):
    if not student.strip():
        return False
    s = _normalise(student)
    return any(_same_answer(s, _normalise(a)) for a in accepted)


def mark(quiz, answers):
    """Return list of (answer_text, result) where result is True/False/None (None = teacher marks)."""
    out = []
    for i, q in enumerate(quiz["questions"]):
        given = answers.get(f"q{i}", "")[:MAX_ANSWER_LEN]
        if q["kind"] == "choice":
            try:
                idx = int(given)
                text = q["options"][idx]
                result = idx == q["correct_option"]
            except (ValueError, IndexError):
                text, result = "", False
        elif q["kind"] == "short":
            text, result = given.strip(), is_correct(given, q["answers"])
        else:
            text, result = given.strip(), None
        out.append((text, result))
    return out


# --------------------------------------------------------------------------
# Results files (CSV, opens in Excel)
# --------------------------------------------------------------------------
#
# Columns: Submitted, Name, Class, Score, Out of, Q1 answer, Q1 mark, Q2 answer, ...
# A mark is 1 (right), 0 (wrong) or "check" (written answer not marked yet).
# "Out of" counts the questions that have a 1 or 0.

def safe_cell(value):
    """Stop spreadsheet formula injection (e.g. a student typing =HYPERLINK(...))."""
    value = str(value).replace("\r", " ").replace("\n", " ")
    if value[:1] in ("=", "+", "@", "\t") or (value[:1] == "-" and _to_number(value) is None):
        return "'" + value
    return value


def unsafe_cell(value):
    """Undo safe_cell for display and re-marking."""
    return value[1:] if value[:1] == "'" and value[1:2] in ("=", "+", "@", "\t", "-") else value


def results_path(quiz_id):
    return os.path.join(RESULTS_DIR, f"{quiz_id}.csv")


def results_header(quiz):
    header = ["Submitted", "Name", "Class", "Score", "Out of"]
    for i in range(1, len(quiz["questions"]) + 1):
        header += [f"Q{i} answer", f"Q{i} mark"]
    return header


def answer_col(i):
    return 5 + 2 * i


def mark_col(i):
    return 6 + 2 * i


def rescore(row, n_questions):
    marks = [row[mark_col(i)] for i in range(n_questions) if mark_col(i) < len(row)]
    row[3] = str(marks.count("1"))
    row[4] = str(sum(1 for m in marks if m in ("0", "1")))


def save_result(quiz, name, group, marked):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    header = results_header(quiz)

    auto = [r for _, r in marked if r is not None]
    row = [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), safe_cell(name), safe_cell(group),
           sum(1 for r in auto if r), len(auto)]
    for text, result in marked:
        row += [safe_cell(text), "check" if result is None else (1 if result else 0)]

    path = results_path(quiz["id"])
    with write_lock:
        new_file = not os.path.exists(path)
        if not new_file:
            with open(path, encoding="utf-8-sig", newline="") as f:
                existing = next(csv.reader(f), None)
            if existing != header:
                # questions were added or removed after people answered: keep old results, start a new file
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                os.rename(path, path[:-4] + f"-before-{stamp}.csv")
                new_file = True
        with open(path, "a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(header)
            w.writerow(row)


def read_results(quiz_id):
    path = results_path(quiz_id)
    if not os.path.exists(path):
        return [], []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    return (rows[0], rows[1:]) if rows else ([], [])


def write_results(quiz_id, header, rows):
    """Rewrite the whole file (call with write_lock held). Fails if Excel has it locked."""
    path = results_path(quiz_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    os.replace(tmp, path)


class ResultsChanged(ValueError):
    pass


def set_mark(quiz, row_index, submitted, q_index, value):
    """Teacher changes one mark. Returns the row's new (score, out_of)."""
    n = len(quiz["questions"])
    if not 0 <= q_index < n or value not in ("1", "0", "check"):
        raise ValueError("That isn't a valid mark.")
    if value == "check" and quiz["questions"][q_index]["kind"] != "written":
        raise ValueError("That isn't a valid mark.")
    with write_lock:
        header, rows = read_results(quiz["id"])
        if header != results_header(quiz):
            raise ResultsChanged("The quiz has changed. Refresh the page.")
        if not 0 <= row_index < len(rows) or rows[row_index][0] != submitted:
            raise ResultsChanged("The results file has changed. Refresh the page.")
        row = rows[row_index]
        row += [""] * (len(header) - len(row))
        row[mark_col(q_index)] = value
        rescore(row, n)
        write_results(quiz["id"], header, rows)
        return row[3], row[4]


def remark_changes(quiz, rows):
    """Auto-marked answers whose mark differs from what the quiz file says now.
    Returns [(row_index, question_index, new_mark)]."""
    changes = []
    for r, row in enumerate(rows):
        for i, q in enumerate(quiz["questions"]):
            if q["kind"] == "written" or mark_col(i) >= len(row):
                continue
            text = unsafe_cell(row[answer_col(i)])
            if q["kind"] == "choice":
                ok = q["correct_option"] is not None and text == q["options"][q["correct_option"]]
            else:
                ok = is_correct(text, q["answers"])
            new = "1" if ok else "0"
            if row[mark_col(i)] != new:
                changes.append((r, i, new))
    return changes


def remark_all(quiz):
    """Re-mark every auto-marked answer with the quiz file's current answers.
    Written-answer marks are kept. Returns how many marks changed."""
    with write_lock:
        header, rows = read_results(quiz["id"])
        if header != results_header(quiz):
            return 0
        changes = remark_changes(quiz, rows)
        for r, i, new in changes:
            rows[r][mark_col(i)] = new
        for r in {r for r, _, _ in changes}:
            rescore(rows[r], len(quiz["questions"]))
        if changes:
            write_results(quiz["id"], header, rows)
        return len(changes)


# Finished submissions, so a refresh or a second click doesn't save the answers twice.
recent_lock = threading.Lock()
recent_done = OrderedDict()   # token -> {"quiz", "name", "marked", "order"}
seen_submissions = OrderedDict()   # submission id (from the form) -> token


def remember_submission(sid, data):
    token = secrets.token_urlsafe(12)
    recent_done[token] = data
    if sid:
        seen_submissions[sid] = token
    while len(recent_done) > MAX_RECENT:
        recent_done.popitem(last=False)
    while len(seen_submissions) > MAX_RECENT:
        seen_submissions.popitem(last=False)
    return token


def parse_order(text, n):
    try:
        order = [int(x) for x in text.split(",")]
    except ValueError:
        return list(range(n))
    return order if sorted(order) == list(range(n)) else list(range(n))


# --------------------------------------------------------------------------
# QR code (so students can scan the address). Byte mode, error correction L,
# versions 1-4: up to 78 characters, plenty for http://192.168.100.200:8000
# --------------------------------------------------------------------------

_QR_DATA_CODEWORDS = {1: 19, 2: 34, 3: 55, 4: 80}
_QR_EC_CODEWORDS = {1: 7, 2: 10, 3: 15, 4: 20}


def _gf_tables():
    exp, log = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i], log[x] = x, i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _gf_tables()


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_remainder(data, n):
    gen = [1]
    for i in range(n):                       # gen *= (x - a^i)
        new = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            new[j] ^= c
            new[j + 1] ^= _gf_mul(c, _GF_EXP[i])
        gen = new
    rem = [0] * n
    for b in data:
        factor = b ^ rem[0]
        rem = rem[1:] + [0]
        for j in range(n):
            rem[j] ^= _gf_mul(gen[j + 1], factor)
    return rem


_QR_MASKS = [
    lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


def _qr_penalty(m):
    n, score = len(m), 0
    for line in [row for row in m] + [[m[y][x] for y in range(n)] for x in range(n)]:
        run = 1
        for k in range(1, n + 1):
            if k < n and line[k] == line[k - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        s = "".join("1" if v else "0" for v in line)
        for pattern in ("10111010000", "00001011101"):
            score += 40 * len(re.findall(f"(?={pattern})", s))
    for y in range(n - 1):
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark, total = sum(map(sum, m)), n * n
    return score + 10 * ((abs(dark * 20 - total * 10) + total - 1) // total - 1)


def qr_matrix(text):
    """Return the QR code for text as rows of True (dark) / False (light)."""
    data = text.encode("utf-8")
    for version in range(1, 5):
        capacity = _QR_DATA_CODEWORDS[version]
        if 4 + 8 + 8 * len(data) <= capacity * 8:
            break
    else:
        raise ValueError("Text too long for this QR code generator.")

    bits = []

    def put(value, length):
        bits.extend((value >> i) & 1 for i in reversed(range(length)))

    put(0b0100, 4)            # byte mode
    put(len(data), 8)
    for b in data:
        put(b, 8)
    put(0, min(4, capacity * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(codewords) < capacity:
        codewords.append(pad)
        pad ^= 0xEC ^ 0x11
    codewords += _rs_remainder(codewords, _QR_EC_CODEWORDS[version])

    size = 17 + 4 * version
    dark = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def setf(x, y, on):
        dark[y][x], fixed[y][x] = on, True

    for i in range(size):                    # timing patterns
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):    # finder patterns + separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    setf(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    if version >= 2:                         # one alignment pattern
        c = size - 7
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                setf(c + dx, c + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask):
        info = (1 << 3) | mask               # error correction level L = 01
        rem = info
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        f = ((info << 10) | rem) ^ 0x5412

        def bit(i):
            return ((f >> i) & 1) == 1

        for i in range(6):
            setf(8, i, bit(i))
        setf(8, 7, bit(6))
        setf(8, 8, bit(7))
        setf(7, 8, bit(8))
        for i in range(9, 15):
            setf(14 - i, 8, bit(i))
        for i in range(8):
            setf(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            setf(8, size - 15 + i, bit(i))
        setf(8, size - 8, True)              # always-dark module

    draw_format(0)                           # reserve the format area before placing data

    i, total = 0, len(codewords) * 8
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        upward = ((right + 1) & 2) == 0
        for vert in range(size):
            y = size - 1 - vert if upward else vert
            for x in (right, right - 1):
                if not fixed[y][x] and i < total:
                    dark[y][x] = ((codewords[i >> 3] >> (7 - (i & 7))) & 1) == 1
                    i += 1
        right -= 2

    def apply_mask(mask):
        for y in range(size):
            for x in range(size):
                if not fixed[y][x] and _QR_MASKS[mask](x, y):
                    dark[y][x] = not dark[y][x]

    best = None
    for mask in range(8):
        apply_mask(mask)
        draw_format(mask)
        penalty = _qr_penalty(dark)
        if best is None or penalty < best[0]:
            best = (penalty, mask)
        apply_mask(mask)                     # undo
    apply_mask(best[1])
    draw_format(best[1])
    return dark


def qr_svg(text, size):
    try:
        m = qr_matrix(text)
    except ValueError:
        return ""
    n, q = len(m), 4
    path = "".join(f"M{x + q},{y + q}h1v1h-1z" for y, row in enumerate(m) for x, on in enumerate(row) if on)
    return (f"<svg class='qr' xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {n + 2 * q} {n + 2 * q}' "
            f"style='width:{size};height:{size}' shape-rendering='crispEdges' role='img' "
            f"aria-label='QR code for {esc(text)}'><rect width='100%' height='100%' fill='#fff'/>"
            f"<path d='{path}' fill='#000'/></svg>")


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
# One stylesheet for every page. System fonts only, nothing loaded from the
# internet. Follows the device's light/dark setting.

STYLE = """
:root {
  color-scheme: light dark;
  --bg:#f4f6fb; --surface:#fff; --surface-2:#f7f8fc; --text:#172033; --muted:#5c667a;
  --line:#e3e7ef; --line-strong:#c9d0dd;
  --accent:#3b5bdb; --accent-hover:#3249b8; --accent-soft:#edf2ff; --on-accent:#fff;
  --ok:#2b8a3e; --ok-soft:#ebfbee; --bad:#c92a2a; --bad-soft:#fff0f0;
  --warn:#a86500; --warn-soft:#fff8e1; --warn-line:#f2d48a;
  --radius:14px; --shadow:0 1px 2px rgba(16,24,40,.05), 0 1px 3px rgba(16,24,40,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0e131d; --surface:#161c28; --surface-2:#1c2331; --text:#e6eaf2; --muted:#9aa4b7;
    --line:#273041; --line-strong:#3a4558;
    --accent:#7c93f6; --accent-hover:#97a9f8; --accent-soft:#1d2645; --on-accent:#0e131d;
    --ok:#5fcf75; --ok-soft:#14291b; --bad:#ff8a8a; --bad-soft:#331a1c;
    --warn:#f0b84a; --warn-soft:#2b2414; --warn-line:#5c4a1c; --shadow:none;
  }
}
* { box-sizing:border-box; }
[hidden] { display:none !important; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; font:17px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
       background:var(--bg); color:var(--text); }
a { color:var(--accent); }
h1 { font-size:clamp(1.5rem,4vw,1.9rem); line-height:1.2; margin:0 0 .35em; letter-spacing:-.01em; }
h2 { font-size:1.15rem; margin:0 0 .8em; }
.muted { color:var(--muted); }
.small { font-size:.88rem; }
.lead { color:var(--muted); font-size:1.05rem; margin:.2em 0 1.4em; }
:focus-visible { outline:3px solid var(--accent); outline-offset:2px; }

/* top bar */
.top { background:var(--surface); border-bottom:1px solid var(--line); }
.top-in { max-width:760px; margin:0 auto; padding:12px 16px; display:flex; align-items:center; gap:12px; }
.top-in.wide { max-width:1200px; }
.brand { display:flex; align-items:center; gap:10px; font-weight:700; color:var(--text); text-decoration:none; }
.tag { font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--accent);
       background:var(--accent-soft); padding:3px 9px; border-radius:999px; }
.top nav { margin-left:auto; display:flex; gap:2px; }
.top nav a { color:var(--muted); text-decoration:none; font-weight:600; font-size:.95rem; padding:6px 10px; border-radius:8px; }
.top nav a:hover { background:var(--surface-2); color:var(--text); }

main { max-width:760px; margin:0 auto; padding:28px 16px 80px; }
main.wide { max-width:1200px; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
        padding:20px 22px; margin:0 0 16px; box-shadow:var(--shadow); }
.card.flush { padding:0; overflow:hidden; }
.card > h2:first-child { margin-top:0; }

/* form fields */
.field { display:block; margin:0 0 14px; }
.field > span { display:block; font-weight:600; font-size:.95rem; margin-bottom:6px; }
.field em { font-style:normal; font-weight:400; color:var(--muted); }
.grid2 { display:grid; gap:0 16px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); }
input[type=text], textarea, select {
  width:100%; font:inherit; color:inherit; background:var(--surface); padding:11px 13px;
  border:1.5px solid var(--line-strong); border-radius:10px; transition:border-color .15s, box-shadow .15s; }
input[type=text]:focus, textarea:focus, select:focus {
  outline:none; border-color:var(--accent); box-shadow:0 0 0 4px var(--accent-soft); }
textarea { min-height:110px; resize:vertical; }

/* buttons */
button, .btn { display:inline-flex; align-items:center; justify-content:center; gap:8px; font:inherit;
  font-weight:650; line-height:1.2; padding:12px 22px; border-radius:10px; border:1.5px solid var(--accent);
  background:var(--accent); color:var(--on-accent); cursor:pointer; text-decoration:none;
  transition:background .15s, border-color .15s; }
button:hover, .btn:hover { background:var(--accent-hover); border-color:var(--accent-hover); }
button:disabled { opacity:.55; cursor:default; }
.ghost { background:transparent; color:var(--accent); }
.ghost:hover { background:var(--accent-soft); border-color:var(--accent); }
.sm { padding:6px 14px; font-size:.9rem; border-radius:8px; }
form.inline { display:inline; }

/* list of quizzes */
.quiz-list { display:grid; gap:12px; }
.quiz-link { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:18px 22px;
  background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); text-decoration:none;
  color:var(--text); box-shadow:var(--shadow); transition:border-color .15s, transform .15s; }
.quiz-link:hover { border-color:var(--accent); transform:translateY(-1px); }
.quiz-link strong { display:block; font-size:1.1rem; }
.chev { font-size:1.4rem; color:var(--accent); }
.empty { text-align:center; padding:40px 20px; border:1.5px dashed var(--line-strong); border-radius:var(--radius); }
.empty p { margin:.3em 0; }

/* questions */
.quiz-head { margin-bottom:22px; }
.meta { font-size:.9rem; color:var(--muted); margin:0; }
.q-head { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
.qnum { flex:none; display:inline-grid; place-items:center; width:30px; height:30px; border-radius:50%;
  background:var(--surface-2); border:1.5px solid var(--line-strong); font-weight:700; font-size:.9rem; color:var(--muted);
  transition:background .2s, color .2s, border-color .2s; }
.q.answered .qnum { background:var(--accent); border-color:var(--accent); color:var(--on-accent); }
.kind { margin-left:auto; font-size:.75rem; font-weight:700; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
.qtext { white-space:pre-wrap; font-size:1.12rem; margin:0 0 14px; }
.opts { display:grid; gap:8px; }
label.opt { display:flex; align-items:center; gap:12px; padding:13px 16px; border:1.5px solid var(--line-strong);
  border-radius:12px; cursor:pointer; transition:border-color .15s, background .15s; }
label.opt:hover { border-color:var(--accent); }
label.opt:has(input:checked) { border-color:var(--accent); background:var(--accent-soft); font-weight:600; }
label.opt input { width:20px; height:20px; margin:0; flex:none; accent-color:var(--accent); }

/* submit bar that stays at the bottom of the screen */
.submitbar { position:sticky; bottom:0; z-index:5; margin:24px -16px -80px;
  padding:14px 16px calc(14px + env(safe-area-inset-bottom)); display:flex; align-items:center; gap:16px;
  background:var(--bg); background:color-mix(in srgb, var(--bg) 90%, transparent);
  -webkit-backdrop-filter:blur(8px); backdrop-filter:blur(8px); border-top:1px solid var(--line); }
.progress { flex:1; min-width:0; }
.progress-text { font-size:.9rem; color:var(--muted); margin-bottom:6px; }
.track { height:8px; background:var(--line); border-radius:999px; overflow:hidden; }
.fill { height:100%; width:0; background:var(--accent); border-radius:999px; transition:width .25s; }

/* student's results */
.hero { display:flex; align-items:center; gap:24px; flex-wrap:wrap; }
.hero h1 { margin-bottom:.15em; }
.hero p { margin:.2em 0; }
.ring { width:112px; height:112px; border-radius:50%; flex:none; display:grid; place-items:center;
  background:conic-gradient(var(--accent) calc(var(--p) * 1%), var(--line) 0); }
.ring span { display:grid; place-items:center; width:86px; height:86px; border-radius:50%;
  background:var(--surface); font-weight:800; font-size:1.45rem; }
.score { font-size:1.5rem; font-weight:800; }
.section-title { margin:30px 0 12px; }
.review { border-left:5px solid var(--line-strong); }
.review.right { border-left-color:var(--ok); }
.review.wrong { border-left-color:var(--bad); }
.review.pending { border-left-color:var(--warn); }
.chip { font-size:.8rem; font-weight:700; padding:3px 10px; border-radius:999px; background:var(--surface-2); color:var(--muted); }
.chip.right { background:var(--ok-soft); color:var(--ok); }
.chip.wrong { background:var(--bad-soft); color:var(--bad); }
.chip.pending { background:var(--warn-soft); color:var(--warn); }
.ans { display:grid; grid-template-columns:max-content 1fr; gap:4px 16px; margin:0; }
.ans dt { color:var(--muted); font-size:.92rem; padding-top:1px; }
.ans dd { margin:0; font-weight:600; white-space:pre-wrap; word-break:break-word; }
.blank { color:var(--muted); font-style:italic; font-weight:400; }

/* one-message pages */
.message { text-align:center; max-width:520px; margin:40px auto; background:var(--surface); border:1px solid var(--line);
  border-radius:var(--radius); padding:36px 28px; box-shadow:var(--shadow); }
.message p { margin:.5em 0; }
.message-icon { width:52px; height:52px; margin:0 auto 14px; border-radius:50%; display:grid; place-items:center;
  font-weight:800; font-size:1.5rem; background:var(--accent-soft); color:var(--accent); }
.message.ok .message-icon { background:var(--ok-soft); color:var(--ok); }
.message.warn .message-icon { background:var(--warn-soft); color:var(--warn); }
.message.bad .message-icon { background:var(--bad-soft); color:var(--bad); }
.message .btn { margin-top:14px; }

.notice { background:var(--warn-soft); border:1px solid var(--warn-line); border-radius:12px; padding:14px 18px; margin:0 0 16px; }
.notice.info { background:var(--accent-soft); border-color:transparent; }
.notice form { margin-top:10px; }

/* teacher pages */
.page-head { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; flex-wrap:wrap; margin-bottom:18px; }
.page-head h1 { margin:0; }
.page-head p { margin:.2em 0 0; }
.head-actions { display:flex; align-items:center; gap:10px; }
.back { display:inline-block; margin-bottom:12px; text-decoration:none; font-weight:600; font-size:.95rem; }
.share { display:flex; gap:24px; align-items:center; flex-wrap:wrap; }
.share .qr { flex:none; border-radius:10px; border:1px solid var(--line); }
.share-text { flex:1; min-width:240px; }
.share-text p { margin:0; }
.eyebrow { font-size:.75rem; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
.url { font-size:clamp(1.4rem,3.5vw,2rem); font-weight:800; letter-spacing:-.01em; word-break:break-all; margin:2px 0 6px; }
.actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }
.pill { display:inline-flex; align-items:center; gap:6px; font-size:.82rem; font-weight:700; padding:4px 10px;
  border-radius:999px; background:var(--surface-2); color:var(--muted); white-space:nowrap; }
.pill::before { content:""; width:7px; height:7px; border-radius:50%; background:currentColor; }
.pill.open { background:var(--ok-soft); color:var(--ok); }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin-bottom:16px; }
.stat { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); padding:16px 18px; box-shadow:var(--shadow); }
.stat b { display:block; font-size:1.7rem; line-height:1.2; font-variant-numeric:tabular-nums; }
.stat span { color:var(--muted); font-size:.88rem; }
.stat.attention { background:var(--warn-soft); border-color:var(--warn-line); }
.footnote { color:var(--muted); font-size:.88rem; margin:6px 2px 14px; }
.footnote code { font-size:.82rem; background:var(--surface-2); padding:2px 6px; border-radius:5px; word-break:break-all; }

table { border-collapse:separate; border-spacing:0; width:100%; font-size:.93rem; }
th, td { padding:10px 14px; text-align:left; vertical-align:top; border-bottom:1px solid var(--line); }
thead th { font-size:.75rem; text-transform:uppercase; letter-spacing:.05em; color:var(--muted);
  background:var(--surface-2); font-weight:700; white-space:nowrap; }
tbody tr:last-child > * { border-bottom:0; }
.num { text-align:right; font-variant-numeric:tabular-nums; }
td.right { text-align:right; }
.strong { font-weight:700; text-decoration:none; }
.strong:hover { text-decoration:underline; }
.scroll { overflow:auto; max-height:72vh; }
.scroll thead th { position:sticky; top:0; z-index:2; }
.students th[scope=row] { position:sticky; left:0; z-index:1; background:var(--surface); white-space:nowrap; }
.students thead th:first-child { left:0; z-index:3; }
.students td { min-width:118px; }
.students td:has(button[data-kind=written]) { min-width:260px; }
.students td.ok { background:var(--ok-soft); }
.students td.bad { background:var(--bad-soft); }
.students td.todo { background:var(--warn-soft); }
.answer-text { white-space:pre-wrap; word-break:break-word; }
button.mk { float:right; width:28px; height:28px; margin:-3px -6px 0 8px; padding:0; border-radius:8px;
  font-size:.9rem; font-weight:800; background:var(--surface); color:var(--muted); border:1.5px solid var(--line-strong); }
button.mk:hover { background:var(--surface-2); border-color:var(--accent); }
td.ok button.mk { color:var(--ok); border-color:var(--ok); }
td.bad button.mk { color:var(--bad); border-color:var(--bad); }
td.todo button.mk { color:var(--warn); border-color:var(--warn); }
.qcell { max-width:520px; white-space:pre-wrap; }
.bar { display:flex; align-items:center; gap:10px; min-width:190px; }
.bar .track { flex:1; height:10px; }
.bar .fill.ok { background:var(--ok); }
.bar .fill.warn { background:var(--warn); }
.bar .fill.bad { background:var(--bad); }
.bar b { min-width:3.2em; text-align:right; font-variant-numeric:tabular-nums; }

/* projector page */
.screen { text-align:center; padding-top:3vh; }
.screen h1 { font-size:clamp(1.6rem,3vw,2.4rem); color:var(--muted); font-weight:600; margin:0; }
.bigurl { font-size:clamp(2.2rem,6.5vw,5rem); font-weight:800; letter-spacing:-.02em; word-break:break-all; line-height:1.1; }
.screen .qr { display:block; margin:28px auto; border-radius:16px; }
.screen p { font-size:1.4rem; }

@media (max-width:560px) {
  .card { padding:16px; }
  .submitbar { gap:12px; }
  .submitbar button { padding:12px 16px; }
}
@media (prefers-reduced-motion: reduce) { * { transition:none !important; } }
"""

LOGO = ("<svg viewBox='0 0 24 24' width='26' height='26' aria-hidden='true'>"
        "<rect width='24' height='24' rx='7' fill='var(--accent)'/>"
        "<path d='M7 12.5l3.2 3.2L17 9' fill='none' stroke='var(--on-accent)' stroke-width='2.4' "
        "stroke-linecap='round' stroke-linejoin='round'/></svg>")


def page(title, body, wide=False, script="", nav="student"):
    """nav: "student", "teacher", or None (no top bar, for the projector)."""
    header = ""
    if nav:
        if nav == "teacher":
            home, extra = "/teacher", ("<span class='tag'>Teacher</span><nav><a href='/teacher'>Quizzes</a>"
                                       "<a href='/teacher/screen' target='_blank'>Projector</a></nav>")
        else:
            home, extra = "/", ""
        header = (f"<header class='top'><div class='top-in{' wide' if wide else ''}'>"
                  f"<a class='brand' href='{home}'>{LOGO}<span>Classroom Quiz</span></a>{extra}</div></header>")
    script = f"<script>{script}</script>" if script else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{html.escape(title)}</title><style>{STYLE}</style></head>
<body>{header}<main class="{'wide' if wide else ''}">{body}</main>{script}</body></html>"""


def message_page(title, heading, text, tone="info", nav="student"):
    """A page with one short message. heading and text are HTML."""
    icon = {"info": "i", "ok": "✓", "warn": "!", "bad": "×"}[tone]
    return page(title, f"<div class='message {tone}'><div class='message-icon' aria-hidden='true'>{icon}</div>"
                       f"<h1>{heading}</h1>{text}</div>", nav=nav)


def esc(s):
    return html.escape(str(s))


def js(value):
    """A Python value as JavaScript, safe inside <script>."""
    return json.dumps(value).replace("</", "<\\/")


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---- student pages ----------------------------------------------------------

def render_home(quizzes):
    open_quizzes = [q for q in quizzes.values() if q["open"]]
    if not open_quizzes:
        items = ("<div class='empty'><p><strong>No quizzes are open right now.</strong></p>"
                 "<p class='muted'>When your teacher opens one, refresh this page.</p></div>")
    else:
        items = "<div class='quiz-list'>" + "".join(
            f"<a class='quiz-link' href='/quiz/{quote(q['id'])}'><span><strong>{esc(q['title'])}</strong>"
            f"<span class='muted'>{plural(len(q['questions']), 'question')}</span></span>"
            "<span class='chev' aria-hidden='true'>→</span></a>"
            for q in open_quizzes) + "</div>"
    return page("Quizzes", f"<h1>Choose your quiz</h1><p class='lead'>Pick the quiz your teacher told you to do.</p>{items}")


def render_closed(quiz, tried_to_submit=False):
    heading = f"{esc(quiz['title'])} is closed"
    if tried_to_submit:
        text = ("<p>Your teacher closed this quiz, so your answers were <strong>not sent</strong>. "
                "Tell your teacher.</p><p class='muted'>Your answers are still saved on this device. If your "
                "teacher opens the quiz again, press Back and submit again.</p>")
        return message_page("Quiz closed", heading, text, "bad")
    return message_page("Quiz closed", heading, "<p class='muted'>Your teacher has closed this quiz.</p>"
                        "<a class='btn ghost' href='/'>See the open quizzes</a>", "warn")


def name_fields(rosters):
    if not rosters:
        return ("<section class='card'><h2>About you</h2><div class='grid2'>"
                "<label class='field'><span>Your full name</span>"
                f"<input type='text' name='name' required maxlength='{MAX_NAME_LEN}'></label>"
                "<label class='field'><span>Class <em>(optional)</em></span>"
                "<input type='text' name='group' maxlength='40'></label></div></section>")
    classes = "".join(f"<option>{esc(c)}</option>" for c in rosters)
    return ("<section class='card'><h2>About you</h2><div class='grid2'>"
            "<label class='field'><span>Your class</span>"
            f"<select name='group' id='group'><option value=''>Choose your class</option>{classes}"
            "<option value='__other'>My class isn't here</option></select></label>"
            "<div id='pickwrap' hidden><label class='field'><span>Your name</span>"
            "<select name='name_pick' id='pick'></select></label></div></div>"
            "<div id='typewrap'><label class='field'><span>Your full name</span>"
            f"<input type='text' name='name' maxlength='{MAX_NAME_LEN}'></label></div></section>")


# Saves answers on the student's device as they type (so a refresh or lost
# Wi-Fi doesn't lose work), fills in the class list, shows progress, and stops
# double submits.
QUIZ_SCRIPT = r"""
(function () {
  var form = document.getElementById('quiz');
  var key = 'quiz:' + form.dataset.quiz;
  var ROSTER = __ROSTER__;
  var group = document.getElementById('group'), pick = document.getElementById('pick');
  var pickwrap = document.getElementById('pickwrap'), typewrap = document.getElementById('typewrap');
  var nameInput = form.elements.name, button = form.querySelector('button[type=submit]');
  var cards = form.querySelectorAll('.q'), answeredText = document.getElementById('answered');
  var fill = document.getElementById('fill'), answered = 0;
  var saved = {};
  try { saved = JSON.parse(localStorage.getItem(key) || '{}') || {}; } catch (e) {}

  function save() {
    var d = {};
    for (var i = 0; i < form.elements.length; i++) {
      var el = form.elements[i];
      if (!el.name || el.type === 'hidden' || el.type === 'submit') continue;
      if (el.type === 'radio') { if (el.checked) d[el.name] = el.value; }
      else d[el.name] = el.value;
    }
    try { localStorage.setItem(key, JSON.stringify(d)); } catch (e) {}
  }

  function progress() {
    answered = 0;
    cards.forEach(function (card) {
      var el = form.elements[card.dataset.q];
      var has = !el ? false : (el.type === 'radio' ? el.checked : (el.value || '').trim() !== '');
      card.classList.toggle('answered', has);
      if (has) answered++;
    });
    answeredText.textContent = answered;
    fill.style.width = (cards.length ? 100 * answered / cards.length : 0) + '%';
  }

  for (var i = 0; i < form.elements.length; i++) {
    var el = form.elements[i];
    if (!el.name || !(el.name in saved) || el.type === 'hidden' || el === pick) continue;
    if (el.type === 'radio') el.checked = el.value === saved[el.name];
    else el.value = saved[el.name];
  }

  if (group && pick) {
    var fillNames = function () {
      var list = ROSTER[group.value];
      pick.length = 0;
      if (list) {
        pick.add(new Option('Choose your name', ''));
        list.forEach(function (n) { pick.add(new Option(n, n)); });
        pick.add(new Option("My name isn't here", '__type'));
      }
      pickwrap.hidden = !list;
    };
    var sync = function () {
      var list = ROSTER[group.value];
      var typing = list ? pick.value === '__type' : group.value !== '';
      if (typing && typewrap.hidden) nameInput.value = '';
      typewrap.hidden = !typing;
      if (!typing) nameInput.value = list && pick.value ? pick.value : '';
    };
    fillNames();
    if (saved.name_pick) pick.value = saved.name_pick;
    sync();
    group.addEventListener('change', function () { fillNames(); sync(); });
    pick.addEventListener('change', sync);
  }

  progress();
  form.addEventListener('input', function () { save(); progress(); });
  form.addEventListener('change', function () { save(); progress(); });
  form.addEventListener('submit', function (ev) {
    if (!nameInput.value.trim()) {
      ev.preventDefault();
      alert(group ? 'Choose your class and name before submitting.' : 'Type your name before submitting.');
      (group || nameInput).focus();
      return;
    }
    var blank = cards.length - answered;
    var warning = blank ? 'You have ' + blank + ' unanswered question' + (blank === 1 ? '' : 's') + '. ' : '';
    if (!confirm(warning + 'Submit your answers? You cannot change them after this.')) { ev.preventDefault(); return; }
    save();
    button.disabled = true;
  });
  window.addEventListener('pageshow', function () { button.disabled = false; });
})();
"""

KIND_LABEL = {"choice": "Choose one", "short": "Short answer", "written": "Explain"}


def render_quiz(quiz, rosters, error=""):
    n = len(quiz["questions"])
    order = list(range(n))
    if quiz["shuffle"] in ("both", "questions"):
        random.shuffle(order)
    shuffle_options = quiz["shuffle"] in ("both", "options")

    parts = [f"<div class='quiz-head'><h1>{esc(quiz['title'])}</h1>"]
    if quiz["instructions"]:
        parts.append(f"<p class='lead' style='margin-bottom:.4em'>{esc(quiz['instructions'])}</p>")
    parts.append(f"<p class='meta'>{plural(n, 'question')}</p></div>")
    if error:
        parts.append(f"<div class='notice' role='alert'>{esc(error)}</div>")
    parts.append(f"<form id='quiz' method='post' action='/quiz/{quote(quiz['id'])}/submit' "
                 f"autocomplete='off' data-quiz='{esc(quiz['id'])}'>"
                 f"<input type='hidden' name='sid' value='{secrets.token_urlsafe(12)}'>"
                 f"<input type='hidden' name='v' value='{esc(quiz['version'])}'>"
                 f"<input type='hidden' name='order' value='{','.join(map(str, order))}'>")
    parts.append(name_fields(rosters))
    for pos, i in enumerate(order):
        q = quiz["questions"][i]
        parts.append(f"<section class='card q' data-q='q{i}'><div class='q-head'>"
                     f"<span class='qnum' aria-hidden='true'>{pos + 1}</span>"
                     f"<span class='muted small'>Question {pos + 1} of {n}</span>"
                     f"<span class='kind'>{KIND_LABEL[q['kind']]}</span></div>"
                     f"<div class='qtext' id='qt{i}'>{esc(q['text'])}</div>")
        if q["kind"] == "choice":
            options = list(range(len(q["options"])))
            if shuffle_options:
                random.shuffle(options)
            parts.append(f"<div class='opts' role='radiogroup' aria-labelledby='qt{i}'>")
            for j in options:
                parts.append(f"<label class='opt'><input type='radio' name='q{i}' value='{j}'>"
                             f"<span>{esc(q['options'][j])}</span></label>")
            parts.append("</div>")
        elif q["kind"] == "short":
            parts.append(f"<input type='text' name='q{i}' maxlength='{MAX_ANSWER_LEN}' "
                         f"placeholder='Your answer' aria-labelledby='qt{i}'>")
        else:
            parts.append(f"<textarea name='q{i}' maxlength='{MAX_ANSWER_LEN}' "
                         f"placeholder='Write your answer' aria-labelledby='qt{i}'></textarea>")
        parts.append("</section>")
    parts.append("<div class='submitbar'><div class='progress'>"
                 f"<div class='progress-text'><span id='answered'>0</span> of {n} answered</div>"
                 "<div class='track'><div class='fill' id='fill'></div></div></div>"
                 "<button type='submit'>Submit answers</button></div></form>")
    return page(quiz["title"], "".join(parts), script=QUIZ_SCRIPT.replace("__ROSTER__", js(rosters)))


def render_submitted(done):
    quiz, name, marked, order = done["quiz"], done["name"], done["marked"], done["order"]
    auto = [r for _, r in marked if r is not None]
    score = sum(1 for r in auto if r)
    pending = len(marked) - len(auto)

    hero = ["<section class='card hero'>"]
    if auto:
        pct = round(100 * score / len(auto))
        hero.append(f"<div class='ring' style='--p:{pct}' role='img' aria-label='{pct}% correct'>"
                    f"<span>{pct}%</span></div>")
    hero.append(f"<div><h1>Thanks, {esc(name)}!</h1><p>Your answers have been sent to your teacher.</p>")
    if auto:
        hero.append(f"<p><span class='score'>{score} / {len(auto)}</span> <span class='muted'>correct</span></p>")
    if pending:
        hero.append(f"<p class='muted small'>Your teacher will mark {plural(pending, 'written answer')}.</p>")
    hero.append("</div></section>")
    parts = ["".join(hero)]

    if quiz["show_answers"]:
        parts.append("<h2 class='section-title'>Your answers</h2>")
        for pos, i in enumerate(order):
            q, (text, result) = quiz["questions"][i], marked[i]
            if result is None:
                cls, label, right = "pending", "Teacher will mark", ""
            elif result:
                cls, label, right = "right", "✓ Correct", ""
            else:
                correct = (q["options"][q["correct_option"]] if q["kind"] == "choice" and q["correct_option"] is not None
                           else (q["answers"][0] if q["answers"] else ""))
                cls, label = "wrong", "✗ Not quite"
                right = f"<dt>Correct answer</dt><dd>{esc(correct)}</dd>"
            given = esc(text) if text else "<span class='blank'>(blank)</span>"
            parts.append(f"<section class='card review {cls}'><div class='q-head'>"
                         f"<span class='qnum' aria-hidden='true'>{pos + 1}</span>"
                         f"<span class='muted small'>Question {pos + 1}</span>"
                         f"<span class='chip {cls}' style='margin-left:auto'>{label}</span></div>"
                         f"<div class='qtext'>{esc(q['text'])}</div>"
                         f"<dl class='ans'><dt>Your answer</dt><dd>{given}</dd>{right}</dl></section>")
    parts.append("<p style='margin-top:24px'><a class='btn ghost' href='/'>Back to quizzes</a></p>")
    clear = f"try {{ localStorage.removeItem({js('quiz:' + quiz['id'])}); }} catch (e) {{}}"
    return page("Submitted", "".join(parts), script=clear)


# ---- teacher pages ------------------------------------------------------------

# Reloads the page when new answers arrive (checks every 5 seconds).
POLL_SCRIPT = """
(function () {
  var sig = __SIG__;
  setInterval(function () {
    if (document.querySelector('button.mk:disabled')) return;
    fetch(__URL__, {cache: 'no-store'}).then(function (r) { return r.ok ? r.text() : sig; })
      .then(function (t) { if (t !== sig) location.reload(); }).catch(function () {});
  }, 5000);
})();
"""

# Click a mark to change it; it saves straight away.
MARK_SCRIPT = r"""
(function () {
  var LABEL = {'1': '✓', '0': '✗', 'check': '?'}, CLS = {'1': 'ok', '0': 'bad', 'check': 'todo'};
  var NAME = {'1': 'right', '0': 'wrong', 'check': 'not marked'};
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('button.mk');
    if (!b || b.disabled) return;
    var next = b.dataset.kind === 'written'
      ? {'check': '1', '1': '0', '0': 'check'}[b.dataset.mark] || '1'
      : (b.dataset.mark === '1' ? '0' : '1');
    b.disabled = true;
    var body = new URLSearchParams({row: b.dataset.row, t: b.dataset.t, q: b.dataset.q, value: next});
    fetch(location.pathname + '/mark', {method: 'POST', body: body})
      .then(function (r) { return r.ok ? r.json() : r.text().then(function (t) { throw new Error(t); }); })
      .then(function (d) {
        b.dataset.mark = next;
        b.textContent = LABEL[next];
        b.setAttribute('aria-label', 'Marked ' + NAME[next] + '. Click to change');
        b.parentNode.className = CLS[next];
        document.getElementById('score-' + b.dataset.row).textContent = d.score + '/' + d.out_of;
      })
      .catch(function (e) { alert('That mark was not saved. ' + (e.message || '')); })
      .then(function () { b.disabled = false; });
  });
})();
"""

COPY_SCRIPT = """
(function () {
  var b = document.getElementById('copy');
  if (!b || !navigator.clipboard) { if (b) b.hidden = true; return; }
  b.addEventListener('click', function () {
    navigator.clipboard.writeText(b.dataset.url).then(function () {
      b.textContent = 'Copied';
      setTimeout(function () { b.textContent = 'Copy link'; }, 1500);
    });
  });
})();
"""

MARK_LABEL = {"1": "✓", "0": "✗", "check": "?"}
MARK_NAME = {"1": "right", "0": "wrong", "check": "not marked"}


def status_signature(quizzes, quiz_id=None):
    if quiz_id is not None:
        return str(len(read_results(quiz_id)[1]))
    return ";".join(f"{q['id']}:{len(read_results(q['id'])[1])}:{int(q['open'])}" for q in quizzes.values())


def poll_script(signature, url):
    return POLL_SCRIPT.replace("__SIG__", js(signature)).replace("__URL__", js(url))


def open_form(quiz, back):
    label, value, cls = ("Close", "0", "sm ghost") if quiz["open"] else ("Open", "1", "sm")
    return (f"<form class='inline' method='post' action='/teacher/quiz/{quote(quiz['id'])}/open'>"
            f"<input type='hidden' name='open' value='{value}'>"
            f"<input type='hidden' name='back' value='{esc(back)}'>"
            f"<button class='{cls}' aria-label='{label} {esc(quiz['title'])}'>{label}</button></form>")


def status_pill(quiz):
    return "<span class='pill open'>Open</span>" if quiz["open"] else "<span class='pill'>Closed</span>"


def render_teacher_home(quizzes, student_url, rosters):
    rows = []
    for q in quizzes.values():
        _, results = read_results(q["id"])
        rows.append(f"<tr><td><a class='strong' href='/teacher/quiz/{quote(q['id'])}'>{esc(q['title'])}</a>"
                    f"<div class='muted small'>{esc(q['id'])}.txt · {plural(len(q['questions']), 'question')}</div></td>"
                    f"<td class='num'>{len(results)}</td><td>{status_pill(q)}</td>"
                    f"<td class='right'>{open_form(q, '/teacher')}</td></tr>")
    if rows:
        table = ("<div class='card flush'><table><thead><tr><th>Quiz</th><th class='num'>Responses</th>"
                 "<th>Status</th><th></th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
                 "<p class='footnote'>Students only see open quizzes. Closing a quiz stops new answers.</p>")
    else:
        table = ("<div class='empty'><p><strong>No quizzes yet.</strong></p><p class='muted'>Put quiz "
                 "<code>.txt</code> files in the <code>quizzes</code> folder, then refresh.</p></div>")
    if rosters:
        lists = ", ".join(f"{esc(c)} ({len(n)} names)" for c, n in rosters.items())
        classes = f"Class lists: {lists}. Students pick their name from the list."
    else:
        classes = ("Students type their own names. To let them pick from a class list, "
                   "see “Class lists” in the README.")
    body = f"""<div class="page-head"><h1>Your quizzes</h1></div>
<section class="card share">{qr_svg(student_url, "150px")}<div class="share-text">
<span class="eyebrow">Students go to</span><div class="url">{esc(student_url)}</div>
<p class="muted small">Post this in your Teams class, or let students scan the code. It works while this
program is running and students are on the school network.</p>
<div class="actions"><a class="btn" href="/teacher/screen" target="_blank">Show on the projector</a>
<button type="button" class="ghost" id="copy" data-url="{esc(student_url)}">Copy link</button></div></div></section>
{table}
<p class="footnote">{classes}</p>
<p class="footnote">Results are saved {RESULTS_WHERE} in <code>{esc(RESULTS_DIR)}</code>.
Open the <code>.csv</code> files with Excel. These teacher pages only open on this computer.</p>"""
    return page("Teacher view", body, wide=True, nav="teacher",
                script=poll_script(status_signature(quizzes), "/teacher/status") + COPY_SCRIPT)


def render_teacher_screen(quizzes, student_url):
    titles = [esc(q["title"]) for q in quizzes.values() if q["open"]]
    listing = (f"<p class='muted'>Then choose <strong>{'</strong> or <strong>'.join(titles)}</strong></p>"
               if titles else "<p class='muted'>No quizzes are open yet.</p>")
    body = (f"<div class='screen'><h1>Go to</h1><div class='bigurl'>{esc(student_url)}</div>"
            f"{qr_svg(student_url, 'min(56vh, 85vw)')}{listing}</div>")
    return page("Quiz address", body, wide=True, nav=None,
                script=poll_script(status_signature(quizzes), "/teacher/status"))


def bar_tone(pct):
    return "ok" if pct >= 70 else "warn" if pct >= 40 else "bad"


def render_teacher_quiz(quiz, notice=""):
    header, results = read_results(quiz["id"])
    n_q = len(quiz["questions"])
    qid = quote(quiz["id"])
    script = poll_script(status_signature({}, quiz["id"]), f"/teacher/status?quiz={qid}")
    parts = ["<a class='back' href='/teacher'>← All quizzes</a>",
             f"<div class='page-head'><div><h1>{esc(quiz['title'])}</h1>"
             f"<p class='muted small'>{esc(quiz['id'])}.txt · {plural(n_q, 'question')}</p></div>"
             f"<div class='head-actions'>{status_pill(quiz)}{open_form(quiz, f'/teacher/quiz/{qid}')}</div></div>"]
    if notice:
        parts.append(f"<div class='notice info' role='status'>{esc(notice)}</div>")
    if not results:
        parts.append("<div class='empty'><p><strong>No responses yet.</strong></p>"
                     "<p class='muted'>This page updates by itself when students submit.</p></div>")
        return page(quiz["title"], "".join(parts), wide=True, nav="teacher", script=script)

    editable = header == results_header(quiz)
    if not editable:
        parts.append("<div class='notice'>Questions were added or removed since these answers were sent, so "
                     "marks can't be changed here. The next answer sent will start a new results file, and "
                     "this one will be kept.</div>")
    else:
        changes = remark_changes(quiz, results)
        if changes:
            confirm = ("Re-mark every auto-marked answer using the quiz file? Marks you changed by hand on "
                       "those questions will be replaced. Written-answer marks are kept.")
            parts.append(
                f"<div class='notice'><strong>{len(changes)} mark(s) don't match the answers in the quiz "
                "file.</strong> This happens when you change an answer in the quiz file after students have "
                "sent theirs, or change a mark yourself on this page."
                f"<form method='post' action='/teacher/quiz/{qid}/remark' "
                f"onsubmit='return confirm({esc(js(confirm))})'>"
                "<button class='sm'>Re-mark using the quiz file</button></form></div>")

    # headline numbers
    percents, to_mark = [], 0
    for r in results:
        try:
            score, out_of = int(r[3]), int(r[4])
        except (ValueError, IndexError):
            continue
        if out_of:
            percents.append(100 * score / out_of)
        to_mark += sum(1 for i in range(n_q) if mark_col(i) < len(r) and r[mark_col(i)] == "check")
    average = f"{round(sum(percents) / len(percents))}%" if percents else "–"
    parts.append("<div class='stats'>"
                 f"<div class='stat'><b>{len(results)}</b><span>Responses</span></div>"
                 f"<div class='stat'><b>{average}</b><span>Average score</span></div>"
                 f"<div class='stat{' attention' if to_mark else ''}'><b>{to_mark}</b>"
                 f"<span>{'Answer' if to_mark == 1 else 'Answers'} to mark</span></div></div>")

    # per-question summary
    rows = []
    for i, q in enumerate(quiz["questions"]):
        marks = [r[mark_col(i)] for r in results if len(r) > mark_col(i)]
        done = [m for m in marks if m in ("0", "1")]
        if not done:
            result = f"<span class='muted'>{'Not marked yet' if q['kind'] == 'written' else '–'}</span>"
        else:
            pct = round(100 * done.count("1") / len(done))
            result = (f"<div class='bar'><div class='track'><div class='fill {bar_tone(pct)}' "
                      f"style='width:{pct}%'></div></div><b>{pct}%</b></div>")
            if len(done) < len(marks):
                result += f"<div class='muted small'>{len(done)} of {len(marks)} marked</div>"
        rows.append(f"<tr><td class='num muted'>{i + 1}</td><td class='qcell'>{esc(q['text'])}</td><td>{result}</td></tr>")
    parts.append("<section class='card flush'><table><thead><tr><th class='num'>#</th><th>Question</th>"
                 "<th>Correct</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></section>")

    # per-student table
    parts.append("<h2 class='section-title'>Students</h2>")
    if editable:
        parts.append("<p class='footnote'>Click ✓, ✗ or ? next to an answer to change its mark. Yellow answers "
                     "still need marking. Changes save straight away to the results file.</p>")
    heads = "".join(f"<th title='{esc(q['text'])}'>Q{i + 1}</th>" for i, q in enumerate(quiz["questions"]))
    parts.append("<div class='card flush scroll'><table class='students'><thead><tr><th>Name</th><th>Class</th>"
                 f"<th class='num'>Score</th>{heads}<th>Submitted</th></tr></thead><tbody>")
    for r_i, r in enumerate(results):
        r = r + [""] * (answer_col(n_q) - len(r))
        cells = []
        for i, q in enumerate(quiz["questions"]):
            ans, mk = unsafe_cell(r[answer_col(i)]), r[mark_col(i)]
            cls = {"1": "ok", "0": "bad", "check": "todo"}.get(mk, "")
            button = ""
            if editable:
                button = (f"<button type='button' class='mk' data-row='{r_i}' data-t='{esc(r[0])}' "
                          f"data-q='{i}' data-kind='{q['kind']}' data-mark='{esc(mk)}' "
                          f"aria-label='Marked {MARK_NAME.get(mk, 'not marked')}. Click to change'>"
                          f"{MARK_LABEL.get(mk, '?')}</button>")
            text = esc(ans) if ans else "<span class='blank'>blank</span>"
            cells.append(f"<td class='{cls}'>{button}<span class='answer-text'>{text}</span></td>")
        parts.append(f"<tr><th scope='row'>{esc(unsafe_cell(r[1]))}</th><td>{esc(unsafe_cell(r[2]))}</td>"
                     f"<td class='num' id='score-{r_i}'>{esc(r[3])}/{esc(r[4])}</td>{''.join(cells)}"
                     f"<td class='muted small'>{esc(r[0])}</td></tr>")
    parts.append("</tbody></table></div>")
    parts.append(f"<p class='footnote'>Results file: <code>{esc(results_path(quiz['id']))}</code></p>")
    return page(quiz["title"], "".join(parts), wide=True, nav="teacher",
                script=script + (MARK_SCRIPT if editable else ""))


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ClassroomQuiz"
    student_url = ""

    def log_message(self, fmt, *args):
        pass  # keep the console quiet; submissions are logged below

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def same_origin(self):
        """Stop other websites open on the teacher's laptop from changing marks."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        parts = urlsplit(origin)
        return (parts.hostname in ("localhost", "127.0.0.1", "::1")
                and parts.port == self.server.server_address[1])

    def send_body(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, content, status=200):
        self.send_body(content.encode("utf-8"), "text/html; charset=utf-8", status)

    def send_text(self, text, status=200):
        self.send_body(text.encode("utf-8"), "text/plain; charset=utf-8", status)

    def send_json(self, value, status=200):
        self.send_body(json.dumps(value).encode("utf-8"), "application/json", status)

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def not_found(self):
        self.send_html(message_page("Not found", "Page not found",
                                    "<p class='muted'>Check the address and try again.</p>"
                                    "<a class='btn ghost' href='/'>Back to quizzes</a>", "warn"), 404)

    def teacher_only(self):
        self.send_html(message_page("Teacher only", "Teacher only",
                                    "<p class='muted'>This page can only be opened on the teacher's computer.</p>"
                                    "<a class='btn ghost' href='/'>Back to quizzes</a>", "bad"), 403)

    def read_form(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.send_html(message_page("Error", "Too much to send",
                                        "<p class='muted'>That was too large to send. Shorten your answers "
                                        "and try again.</p>", "bad"), 413)
            return None
        if length == 0:
            return {}
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        return {k: v[0] for k, v in form.items()}

    def do_GET(self):
        path, _, query = self.path.partition("?")
        path = unquote(path)
        quizzes = list_quizzes()

        if path == "/":
            return self.send_html(render_home(quizzes))
        m = re.fullmatch(r"/quiz/([^/]+)", path)
        if m:
            quiz = quizzes.get(m.group(1))
            if not quiz:
                return self.not_found()
            if not quiz["open"]:
                return self.send_html(render_closed(quiz))
            return self.send_html(render_quiz(quiz, read_rosters()))
        m = re.fullmatch(r"/quiz/([^/]+)/done/([A-Za-z0-9_-]+)", path)
        if m:
            with recent_lock:
                done = recent_done.get(m.group(2))
            if done and done["quiz"]["id"] == m.group(1):
                return self.send_html(render_submitted(done))
            return self.send_html(message_page("Sent", "Your answers were sent",
                                               "<a class='btn ghost' href='/'>Back to quizzes</a>", "ok"))

        if path == "/teacher" or path.startswith("/teacher/"):
            if not self.is_local():
                return self.teacher_only()
            if path in ("/teacher", "/teacher/"):
                return self.send_html(render_teacher_home(quizzes, self.student_url, read_rosters()))
            if path == "/teacher/screen":
                return self.send_html(render_teacher_screen(quizzes, self.student_url))
            if path == "/teacher/status":
                quiz_id = parse_qs(query).get("quiz", [None])[0]
                if quiz_id is not None and quiz_id not in quizzes:
                    return self.not_found()
                return self.send_text(status_signature(quizzes, quiz_id))
            m = re.fullmatch(r"/teacher/quiz/([^/]+)", path)
            if m and m.group(1) in quizzes:
                q = parse_qs(query).get("remarked", [None])[0]
                notice = f"Re-marked: {q} mark(s) changed." if q and q.isdigit() else ""
                return self.send_html(render_teacher_quiz(quizzes[m.group(1)], notice))
        return self.not_found()

    def do_POST(self):
        path = unquote(self.path.split("?", 1)[0])
        m = re.fullmatch(r"/quiz/([^/]+)/submit", path)
        if m:
            return self.submit(m.group(1))
        m = re.fullmatch(r"/teacher/quiz/([^/]+)/(mark|remark|open)", path)
        if m:
            if not self.is_local() or not self.same_origin():
                return self.teacher_only()
            quiz = list_quizzes().get(m.group(1))
            if not quiz:
                return self.not_found()
            form = self.read_form()
            if form is None:
                return
            return getattr(self, "teacher_" + m.group(2))(quiz, form)
        return self.not_found()

    def submit(self, quiz_id):
        quiz = list_quizzes().get(quiz_id)
        if not quiz:
            return self.not_found()
        form = self.read_form()
        if form is None:
            return
        done_url = f"/quiz/{quote(quiz_id)}/done/"
        sid = form.get("sid", "")[:40]
        with recent_lock:
            token = seen_submissions.get(sid) if sid else None
        if token:                                  # already saved (second click, Back + submit)
            return self.redirect(done_url + token)
        if not quiz["open"]:
            return self.send_html(render_closed(quiz, tried_to_submit=True), 403)
        if form.get("v", quiz["version"]) != quiz["version"]:
            return self.send_html(render_quiz(quiz, read_rosters(),
                                              "Your teacher changed this quiz while you were doing it. "
                                              "Check your answers, then submit again."), 409)

        name = " ".join(form.get("name", "").split())[:MAX_NAME_LEN]
        group = " ".join(form.get("group", "").split())[:40]
        if group == "__other":
            group = ""
        if not name:
            return self.send_html(render_quiz(quiz, read_rosters(), "Please type your name."), 400)

        marked = mark(quiz, form)
        with recent_lock:
            token = seen_submissions.get(sid) if sid else None
            if not token:
                try:
                    save_result(quiz, name, group, marked)
                except OSError as e:
                    print(f"  !! Could not save {name}'s answers ({e}). "
                          f"If {results_path(quiz_id)} is open in Excel, close it.", flush=True)
                    return self.send_html(message_page(
                        "Not sent", "Your answers were not saved",
                        "<p>Tell your teacher, then press Back and submit again.</p>", "bad"), 503)
                token = remember_submission(sid, {"quiz": quiz, "name": name, "marked": marked,
                                                  "order": parse_order(form.get("order", ""), len(marked))})
                auto = [r for _, r in marked if r is not None]
                print(f"  {datetime.now():%H:%M:%S}  {quiz['title']}: {name} "
                      f"({sum(1 for r in auto if r)}/{len(auto)})", flush=True)
        self.redirect(done_url + token)

    def teacher_mark(self, quiz, form):
        try:
            row, q = int(form.get("row", "")), int(form.get("q", ""))
        except ValueError:
            return self.send_text("That isn't a valid mark.", 400)
        try:
            score, out_of = set_mark(quiz, row, form.get("t", ""), q, form.get("value", ""))
        except ValueError as e:
            return self.send_text(str(e), 409)
        except OSError:
            return self.send_text("Close the results file in Excel, then try again.", 409)
        self.send_json({"score": score, "out_of": out_of})

    def teacher_remark(self, quiz, form):
        try:
            changed = remark_all(quiz)
        except OSError:
            return self.send_html(render_teacher_quiz(
                quiz, "Couldn't re-mark: close the results file in Excel, then try again."), 409)
        self.redirect(f"/teacher/quiz/{quote(quiz['id'])}?remarked={changed}")

    def teacher_open(self, quiz, form):
        set_open(quiz["id"], form.get("open") == "1")
        back = form.get("back", "")
        self.redirect(back if back in ("/teacher", f"/teacher/quiz/{quote(quiz['id'])}") else "/teacher")


# --------------------------------------------------------------------------
# Where results are saved
# --------------------------------------------------------------------------

def read_settings(path):
    """quiz-settings.txt: lines like  results_folder: C:\\Users\\me\\OneDrive - School\\Quiz results"""
    settings = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if ":" in line and not line.startswith("#"):
                    key, value = line.split(":", 1)
                    settings[key.strip().lower()] = value.strip().strip('"')
    except OSError:
        pass
    return settings


def school_onedrive_folders(home, env, system):
    """Folders synced from a school/work OneDrive account. Personal OneDrive is left out."""
    found = []
    if system == "Windows":
        if env.get("OneDriveCommercial"):
            found.append(env["OneDriveCommercial"])
        found += sorted(glob.glob(os.path.join(home, "OneDrive - *")))
    else:
        cloud = sorted(glob.glob(os.path.join(home, "Library", "CloudStorage", "OneDrive-*")))
        found += [p for p in cloud if os.path.basename(p) != "OneDrive-Personal"
                  and not os.path.basename(p).startswith("OneDrive-SharedLibraries")]
        found += sorted(glob.glob(os.path.join(home, "OneDrive - *")))     # older OneDrive versions
    unique, seen = [], set()
    for p in found:
        key = os.path.normcase(os.path.normpath(p))
        if key not in seen and os.path.isdir(p):
            seen.add(key)
            unique.append(os.path.normpath(p))
    return unique


def choose_results_dir(cli_value, settings, home, env, system):
    """Returns (folder, where). Raises ValueError if a folder you named can't be used,
    so results are never quietly saved somewhere you didn't expect."""
    named = cli_value or settings.get("results_folder", "")
    if named:
        folder = os.path.normpath(os.path.join(BASE_DIR, os.path.expanduser(os.path.expandvars(named))))
        if not os.path.isdir(folder):
            raise ValueError(f"The results folder you chose doesn't exist:\n   {folder}\n"
                             "Create it, or fix the path in quiz-settings.txt.")
        if not os.access(folder, os.W_OK):
            raise ValueError(f"Can't save into the results folder you chose:\n   {folder}")
        return folder, "in the folder you chose"

    matches = [os.path.join(d, ONEDRIVE_FOLDER) for d in school_onedrive_folders(home, env, system)
               if os.path.isdir(os.path.join(d, ONEDRIVE_FOLDER))]
    if matches:
        if not os.access(matches[0], os.W_OK):
            raise ValueError(f"Found your OneDrive '{ONEDRIVE_FOLDER}' folder but can't save into it:\n"
                             f"   {matches[0]}\nIn OneDrive, make sure you're signed in and it has finished syncing.")
        return matches[0], "in your school OneDrive"
    return os.path.join(BASE_DIR, "results"), "on this computer"


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no data is sent; just picks the network interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="Classroom quiz server (data stays on this computer).")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--results-dir", help="folder to save results in (default: a 'Quiz results' "
                                              "folder in your school OneDrive if there is one, else ./results)")
    args = parser.parse_args()

    global RESULTS_DIR, RESULTS_WHERE
    try:
        RESULTS_DIR, RESULTS_WHERE = choose_results_dir(args.results_dir, read_settings(SETTINGS_FILE),
                                                        os.path.expanduser("~"), os.environ, platform.system())
    except ValueError as e:
        print(f"\n !! {e}\n")
        sys.exit(1)

    os.makedirs(QUIZ_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    Handler.student_url = f"http://{local_ip()}:{args.port}"
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)

    quizzes = list_quizzes()
    rosters = read_rosters()
    print("=" * 60)
    print(" Classroom Quiz Server is running")
    print("=" * 60)
    print(f" Students go to:   {Handler.student_url}")
    print(f" Teacher view:     http://localhost:{args.port}/teacher")
    print(f" Results saved {RESULTS_WHERE}:")
    print(f"   {RESULTS_DIR}")
    print(f" Quizzes loaded:   {len(quizzes)} ({sum(q['open'] for q in quizzes.values())} open)")
    print(f" Class lists:      {len(rosters) or 'none (students type their names)'}")
    print(" Close this window (or press Ctrl+C) to stop.")
    print("=" * 60, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
