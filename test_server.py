"""Checks for results files, the teacher tools and the web pages. Run with:  python3 test_server.py

Uses temporary folders, so your real quizzes and results are not touched.
"""

import http.client
import os
import re
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import quiz_server as qs

QUIZ = """title: Test quiz
Q: 1 + 1
A: 2
Q: Pick three
- 2
* 3
Q: Explain
"""


class TempFolders(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (qs.QUIZ_DIR, qs.RESULTS_DIR, qs.CLASS_DIR, qs.CLOSED_FILE)
        qs.QUIZ_DIR = os.path.join(self.dir, "quizzes")
        qs.RESULTS_DIR = os.path.join(self.dir, "results")
        qs.CLASS_DIR = os.path.join(self.dir, "classes")
        qs.CLOSED_FILE = os.path.join(self.dir, "closed-quizzes.txt")
        os.makedirs(qs.QUIZ_DIR)
        self.write_quiz(QUIZ)

    def tearDown(self):
        qs.QUIZ_DIR, qs.RESULTS_DIR, qs.CLASS_DIR, qs.CLOSED_FILE = self.saved
        shutil.rmtree(self.dir)

    def write_quiz(self, text):
        with open(os.path.join(qs.QUIZ_DIR, "t.txt"), "w", encoding="utf-8") as f:
            f.write(text)

    def quiz(self):
        return qs.list_quizzes()["t"]

    def submit(self, name, answers):
        quiz = self.quiz()
        qs.save_result(quiz, name, "9SD", qs.mark(quiz, answers))


class Results(TempFolders):
    def test_teacher_marks_written_answer(self):
        self.submit("Ana", {"q0": "2", "q1": "1", "q2": "because"})
        header, rows = qs.read_results("t")
        self.assertEqual(rows[0][3:5], ["2", "2"])
        self.assertEqual(rows[0][qs.mark_col(2)], "check")
        self.assertEqual(qs.set_mark(self.quiz(), 0, rows[0][0], 2, "1"), ("3", "3"))
        self.assertEqual(qs.set_mark(self.quiz(), 0, rows[0][0], 2, "0"), ("2", "3"))
        self.assertEqual(qs.set_mark(self.quiz(), 0, rows[0][0], 2, "check"), ("2", "2"))
        self.assertEqual(qs.set_mark(self.quiz(), 0, rows[0][0], 0, "0"), ("1", "2"))

    def test_bad_marks_are_refused(self):
        self.submit("Ana", {"q0": "2"})
        stamp = qs.read_results("t")[1][0][0]
        with self.assertRaises(ValueError):
            qs.set_mark(self.quiz(), 0, stamp, 0, "check")     # only written answers can be "check"
        with self.assertRaises(ValueError):
            qs.set_mark(self.quiz(), 0, "2000-01-01 00:00:00", 0, "1")
        with self.assertRaises(ValueError):
            qs.set_mark(self.quiz(), 5, stamp, 0, "1")

    def test_file_still_opens_in_excel_format(self):
        self.submit("Ana", {"q0": "2"})
        stamp = qs.read_results("t")[1][0][0]
        qs.set_mark(self.quiz(), 0, stamp, 0, "0")
        with open(qs.results_path("t"), "rb") as f:
            data = f.read()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))      # Excel needs this to read UTF-8
        self.assertIn(b"\r\n", data)

    def test_remark_after_fixing_answer(self):
        self.submit("Ana", {"q0": "3", "q1": "1", "q2": "x"})
        self.submit("Ben", {"q0": "2", "q1": "0", "q2": "y"})
        stamp = qs.read_results("t")[1][0][0]
        qs.set_mark(self.quiz(), 0, stamp, 2, "1")             # teacher marks a written answer
        self.write_quiz(QUIZ.replace("A: 2", "A: 3"))          # teacher fixes the answer key
        self.assertEqual(len(qs.remark_changes(self.quiz(), qs.read_results("t")[1])), 2)
        self.assertEqual(qs.remark_all(self.quiz()), 2)
        rows = qs.read_results("t")[1]
        self.assertEqual([rows[0][qs.mark_col(0)], rows[1][qs.mark_col(0)]], ["1", "0"])
        self.assertEqual(rows[0][qs.mark_col(2)], "1")         # written mark kept
        self.assertEqual(rows[0][3:5], ["3", "3"])
        self.assertEqual(qs.remark_all(self.quiz()), 0)

    def test_remark_reads_protected_cells(self):
        self.write_quiz("Q: Type =1\nA: =1\n")
        self.submit("Ana", {"q0": "=1"})
        rows = qs.read_results("t")[1]
        self.assertEqual(rows[0][qs.answer_col(0)], "'=1")
        self.assertEqual(qs.remark_changes(self.quiz(), rows), [])

    def test_open_and_close(self):
        self.assertTrue(self.quiz()["open"])
        qs.set_open("t", False)
        self.assertFalse(self.quiz()["open"])
        qs.set_open("t", True)
        self.assertTrue(self.quiz()["open"])

    def test_class_lists(self):
        os.makedirs(qs.CLASS_DIR)
        with open(os.path.join(qs.CLASS_DIR, "9SD.txt"), "w", encoding="utf-8") as f:
            f.write("zoe  smith\n\nAna Lee\n# a comment\nAna Lee\n")
        self.assertEqual(qs.read_rosters(), {"9SD": ["Ana Lee", "zoe smith"]})

    def test_version_ignores_answer_fixes(self):
        before = self.quiz()["version"]
        self.write_quiz(QUIZ.replace("A: 2", "A: 2 | two"))
        self.assertEqual(self.quiz()["version"], before)
        self.write_quiz(QUIZ + "Q: Another\nA: 1\n")
        self.assertNotEqual(self.quiz()["version"], before)


class Small(unittest.TestCase):
    def test_parse_order(self):
        self.assertEqual(qs.parse_order("2,0,1", 3), [2, 0, 1])
        self.assertEqual(qs.parse_order("2,2,1", 3), [0, 1, 2])
        self.assertEqual(qs.parse_order("junk", 2), [0, 1])

    def test_shuffle_setting(self):
        for value, expected in (("yes", "both"), ("questions", "questions"), ("options", "options"), ("no", "no")):
            self.assertEqual(qs.parse_quiz(f"shuffle: {value}\nQ: a\nA: 1")["shuffle"], expected)

    def test_qr_shape(self):
        m = qs.qr_matrix("http://10.5.0.56:8000")
        self.assertEqual(len(m), 25)                            # version 2
        finder = [[True] * 7, [True] + [False] * 5 + [True]]
        self.assertEqual(m[0][:7], finder[0])
        self.assertEqual(m[1][:7], finder[1])
        self.assertEqual(m[0][-7:], finder[0])
        self.assertEqual(m[-7][:7], finder[0])
        self.assertTrue(m[25 - 8][8])                           # always-dark module
        self.assertEqual(len(qs.qr_matrix("http://192.168.100.200:65535")), 25)
        with self.assertRaises(ValueError):
            qs.qr_matrix("x" * 100)


class ResultsFolder(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.home)

    def make(self, *parts):
        path = os.path.join(self.home, *parts)
        os.makedirs(path)
        return path

    def choose(self, system="Darwin", env=None, settings=None, cli=None):
        return qs.choose_results_dir(cli, settings or {}, self.home, env or {}, system)

    def test_mac_school_onedrive(self):
        self.make("Library", "CloudStorage", "OneDrive-Personal", "Quiz results")
        school = self.make("Library", "CloudStorage", "OneDrive-Westlake School", "Quiz results")
        self.assertEqual(self.choose(), (school, "in your school OneDrive"))

    def test_mac_personal_onedrive_is_never_used(self):
        self.make("Library", "CloudStorage", "OneDrive-Personal", "Quiz results")
        self.assertEqual(self.choose()[1], "on this computer")

    def test_needs_the_quiz_results_folder(self):
        self.make("Library", "CloudStorage", "OneDrive-Westlake School", "Documents")
        folder, where = self.choose()
        self.assertEqual(where, "on this computer")
        self.assertEqual(folder, os.path.join(qs.BASE_DIR, "results"))

    def test_windows_school_onedrive(self):
        school = self.make("OneDrive - Westlake School")
        os.makedirs(os.path.join(school, "Quiz results"))
        self.make("OneDrive", "Quiz results")               # personal OneDrive on Windows
        env = {"OneDriveCommercial": school, "OneDriveConsumer": os.path.join(self.home, "OneDrive")}
        self.assertEqual(self.choose("Windows", env), (os.path.join(school, "Quiz results"), "in your school OneDrive"))
        self.assertEqual(len(qs.school_onedrive_folders(self.home, env, "Windows")), 1)   # no duplicates

    def test_folder_you_name_wins(self):
        self.make("Library", "CloudStorage", "OneDrive-Westlake School", "Quiz results")
        mine = self.make("somewhere")
        self.assertEqual(self.choose(settings={"results_folder": mine}), (mine, "in the folder you chose"))
        other = self.make("other")
        self.assertEqual(self.choose(settings={"results_folder": mine}, cli=other)[0], other)

    def test_missing_named_folder_stops_the_server(self):
        with self.assertRaises(ValueError):
            self.choose(settings={"results_folder": os.path.join(self.home, "nope")})

    def test_settings_file(self):
        path = os.path.join(self.home, "quiz-settings.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write('# comment\nresults_folder: "C:\\Users\\me\\OneDrive - School\\Quiz results"\n')
        self.assertEqual(qs.read_settings(path), {"results_folder": "C:\\Users\\me\\OneDrive - School\\Quiz results"})
        self.assertEqual(qs.read_settings(os.path.join(self.home, "missing.txt")), {})

    def test_server_refuses_to_start_with_bad_folder(self):
        import subprocess
        import sys
        run = subprocess.run([sys.executable, qs.__file__, "--results-dir", os.path.join(self.home, "nope")],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 1)
        self.assertIn("doesn't exist", run.stdout)


class WebPages(TempFolders):
    def setUp(self):
        super().setUp()
        qs.recent_done.clear()
        qs.seen_submissions.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), qs.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, form=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = urlencode(form) if form is not None else None
        h = {"Content-Type": "application/x-www-form-urlencoded"} if form is not None else {}
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read().decode("utf-8")
        conn.close()
        return r.status, r.getheader("Location"), data

    def quiz_form(self):
        status, _, page = self.request("GET", "/quiz/t")
        self.assertEqual(status, 200)
        return {name: value for name, value in re.findall(r"type='hidden' name='(\w+)' value='([^']*)'", page)}

    def test_submit_once_even_if_sent_twice(self):
        form = dict(self.quiz_form(), name="Ana", group="9SD", q0="2", q1="1", q2="hi")
        s1, loc1, _ = self.request("POST", "/quiz/t/submit", form)
        s2, loc2, _ = self.request("POST", "/quiz/t/submit", form)
        self.assertEqual((s1, s2), (303, 303))
        self.assertEqual(loc1, loc2)
        self.assertEqual(len(qs.read_results("t")[1]), 1)
        status, _, page = self.request("GET", loc1)
        self.assertIn("Thanks, Ana!", page)
        self.assertIn("2 / 2", page)
        self.assertIn("localStorage.removeItem", page)

    def test_changed_quiz_asks_to_check(self):
        form = dict(self.quiz_form(), name="Ana", q0="2")
        self.write_quiz(QUIZ.replace("1 + 1", "2 + 2"))
        status, _, page = self.request("POST", "/quiz/t/submit", form)
        self.assertEqual(status, 409)
        self.assertIn("changed this quiz", page)
        self.assertEqual(qs.read_results("t")[1], [])

    def test_closed_quiz(self):
        form = dict(self.quiz_form(), name="Ana", q0="2")
        qs.set_open("t", False)
        self.assertNotIn("/quiz/t", self.request("GET", "/")[2])
        self.assertIn("is closed", self.request("GET", "/quiz/t")[2])
        status, _, page = self.request("POST", "/quiz/t/submit", form)
        self.assertEqual(status, 403)
        self.assertIn("not sent", page)

    def test_teacher_marking_and_origin_check(self):
        self.request("POST", "/quiz/t/submit", dict(self.quiz_form(), name="Ana", q0="2", q2="hi"))
        stamp = qs.read_results("t")[1][0][0]
        mark = {"row": "0", "t": stamp, "q": "2", "value": "1"}
        status, _, _ = self.request("POST", "/teacher/quiz/t/mark", mark, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _, body = self.request("POST", "/teacher/quiz/t/mark", mark,
                                       {"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(status, 200)
        self.assertEqual(body, '{"score": "2", "out_of": "3"}')   # Q2 was left blank
        page = self.request("GET", "/teacher/quiz/t")[2]
        self.assertIn("class='mk'", page)
        self.assertEqual(self.request("GET", "/teacher/status?quiz=t")[2], "1")

    def test_remark_button(self):
        self.request("POST", "/quiz/t/submit", dict(self.quiz_form(), name="Ana", q0="3"))
        self.write_quiz(QUIZ.replace("A: 2", "A: 3"))
        self.assertIn("don't match", self.request("GET", "/teacher/quiz/t")[2])
        status, loc, _ = self.request("POST", "/teacher/quiz/t/remark", {})   # the real form sends no fields
        self.assertEqual((status, loc), (303, "/teacher/quiz/t?remarked=1"))
        page = self.request("GET", loc)[2]
        self.assertIn("Re-marked: 1 mark(s) changed.", page)
        self.assertNotIn("don't match", page)

    def test_open_close_buttons(self):
        status, loc, _ = self.request("POST", "/teacher/quiz/t/open", {"open": "0", "back": "/teacher"})
        self.assertEqual((status, loc), (303, "/teacher"))
        self.assertFalse(self.quiz()["open"])
        status, loc, _ = self.request("POST", "/teacher/quiz/t/open", {"open": "1", "back": "http://evil"})
        self.assertEqual(loc, "/teacher")
        self.assertTrue(self.quiz()["open"])

    def test_class_list_on_quiz_page(self):
        os.makedirs(qs.CLASS_DIR)
        with open(os.path.join(qs.CLASS_DIR, "9SD.txt"), "w", encoding="utf-8") as f:
            f.write("Ana Lee\n</script><b>x\n")
        page = self.request("GET", "/quiz/t")[2]
        self.assertIn("<option>9SD</option>", page)
        self.assertIn('"Ana Lee"', page)
        self.assertNotIn("</script><b>", page)                  # names can't break the page

    def test_teacher_pages_load(self):
        for path in ("/teacher", "/teacher/screen", "/teacher/quiz/t", "/teacher/status"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 200)
        self.assertIn("<svg class='qr'", self.request("GET", "/teacher")[2])


if __name__ == "__main__":
    unittest.main()
