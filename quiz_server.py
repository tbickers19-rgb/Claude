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
import html
import os
import re
import socket
import threading
from datetime import datetime
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUIZ_DIR = os.path.join(BASE_DIR, "quizzes")
RESULTS_DIR = os.path.join(BASE_DIR, "results")

MAX_BODY = 200_000       # bytes accepted in one submission
MAX_ANSWER_LEN = 1000    # characters kept per answer
MAX_NAME_LEN = 80

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
    quiz = {"title": "Quiz", "instructions": "", "show_answers": True, "questions": []}
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


def list_quizzes():
    quizzes = {}
    if not os.path.isdir(QUIZ_DIR):
        return quizzes
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
            quizzes[quiz_id] = quiz
    return quizzes


# --------------------------------------------------------------------------
# Marking
# --------------------------------------------------------------------------

def _to_number(s):
    s = s.replace(",", "").replace(" ", "").replace("−", "-")
    try:
        if re.fullmatch(r"[-+]?\d+/\d+", s):
            return Fraction(s)
        if re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)", s):
            return Fraction(s)
    except (ValueError, ZeroDivisionError):
        return None
    return None


def _normalise(s):
    s = s.strip().lower()
    s = re.sub(r"^[a-z]\s*=\s*", "", s)   # "x = 4" -> "4"
    s = s.rstrip(".")
    return re.sub(r"\s+", " ", s)


def is_correct(student, accepted):
    if not student.strip():
        return False
    s = _normalise(student)
    s_num = _to_number(s)
    for a in accepted:
        a_n = _normalise(a)
        a_num = _to_number(a_n)
        if s_num is not None and a_num is not None:
            if abs(float(s_num) - float(a_num)) < 1e-9:
                return True
        elif s.replace(" ", "") == a_n.replace(" ", ""):
            return True
    return False


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

def safe_cell(value):
    """Stop spreadsheet formula injection (e.g. a student typing =HYPERLINK(...))."""
    value = str(value).replace("\r", " ").replace("\n", " ")
    if value[:1] in ("=", "+", "@", "\t") or (value[:1] == "-" and _to_number(value) is None):
        return "'" + value
    return value


def results_path(quiz_id):
    return os.path.join(RESULTS_DIR, f"{quiz_id}.csv")


def save_result(quiz, name, group, marked):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    header = ["Submitted", "Name", "Class", "Score", "Out of"]
    for i, q in enumerate(quiz["questions"], 1):
        header += [f"Q{i} answer", f"Q{i} mark"]

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
                # quiz was edited after people answered: keep old results, start a new file
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


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

STYLE = """
:root { --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#5b6475; --accent:#2557d6;
        --ok:#1a7f37; --bad:#c62828; --line:#dde1e8; }
* { box-sizing:border-box; }
body { margin:0; font:17px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
       background:var(--bg); color:var(--text); }
main { max-width:760px; margin:0 auto; padding:24px 16px 64px; }
main.wide { max-width:1200px; }
h1 { font-size:1.6rem; margin:.2em 0 .4em; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:18px 20px; margin:14px 0; }
.qnum { color:var(--muted); font-size:.9rem; font-weight:600; }
.qtext { white-space:pre-wrap; margin:.3em 0 .7em; font-size:1.05rem; }
label.opt { display:block; padding:8px 10px; border:1px solid var(--line); border-radius:8px;
            margin:6px 0; cursor:pointer; }
label.opt:hover { border-color:var(--accent); }
input[type=text], textarea { width:100%; font:inherit; padding:9px 11px;
            border:1px solid #b9c0cc; border-radius:8px; }
textarea { min-height:90px; }
button, .btn { background:var(--accent); color:#fff; border:0; border-radius:8px;
            padding:11px 22px; font:inherit; font-weight:600; cursor:pointer; text-decoration:none;
            display:inline-block; }
.muted { color:var(--muted); }
.ok { color:var(--ok); font-weight:600; } .bad { color:var(--bad); font-weight:600; }
.score { font-size:2.4rem; font-weight:700; }
a { color:var(--accent); }
table { border-collapse:collapse; width:100%; font-size:.92rem; background:var(--card); }
th, td { border:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }
th { background:#eef1f6; }
.scroll { overflow-x:auto; }
code { background:#eef1f6; padding:2px 6px; border-radius:5px; }
"""


def page(title, body, wide=False):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{STYLE}</style></head>
<body><main class="{'wide' if wide else ''}">{body}</main></body></html>"""


def esc(s):
    return html.escape(str(s))


def render_home(quizzes):
    if not quizzes:
        items = "<p class='muted'>No quizzes are open right now.</p>"
    else:
        items = "".join(
            f"<div class='card'><a href='/quiz/{quote(q['id'])}'><strong>{esc(q['title'])}</strong></a>"
            f"<div class='muted'>{len(q['questions'])} questions</div></div>"
            for q in quizzes.values())
    return page("Quizzes", f"<h1>Choose your quiz</h1>{items}")


def render_quiz(quiz, error=""):
    parts = [f"<h1>{esc(quiz['title'])}</h1>"]
    if quiz["instructions"]:
        parts.append(f"<p class='muted'>{esc(quiz['instructions'])}</p>")
    if error:
        parts.append(f"<p class='bad'>{esc(error)}</p>")
    parts.append(f"<form method='post' action='/quiz/{quote(quiz['id'])}/submit' autocomplete='off'>")
    parts.append("<div class='card'><label><strong>Your full name</strong><br>"
                 f"<input type='text' name='name' required maxlength='{MAX_NAME_LEN}'></label>"
                 "<br><br><label><strong>Class</strong> <span class='muted'>(optional)</span><br>"
                 "<input type='text' name='group' maxlength='40'></label></div>")
    for i, q in enumerate(quiz["questions"]):
        parts.append(f"<div class='card'><div class='qnum'>Question {i + 1}</div>"
                     f"<div class='qtext'>{esc(q['text'])}</div>")
        if q["kind"] == "choice":
            for j, opt in enumerate(q["options"]):
                parts.append(f"<label class='opt'><input type='radio' name='q{i}' value='{j}'> {esc(opt)}</label>")
        elif q["kind"] == "short":
            parts.append(f"<input type='text' name='q{i}' maxlength='{MAX_ANSWER_LEN}' placeholder='Your answer'>")
        else:
            parts.append(f"<textarea name='q{i}' maxlength='{MAX_ANSWER_LEN}' placeholder='Your answer'></textarea>")
        parts.append("</div>")
    parts.append("<button type='submit' onclick=\"return confirm('Submit your answers? You cannot change them after this.')\">"
                 "Submit answers</button></form>")
    return page(quiz["title"], "".join(parts))


def render_submitted(quiz, name, marked):
    auto = [r for _, r in marked if r is not None]
    score = sum(1 for r in auto if r)
    parts = [f"<h1>Thanks, {esc(name)}!</h1><p>Your answers have been sent to your teacher.</p>"]
    if auto:
        parts.append(f"<div class='card'><div class='muted'>Your score</div>"
                     f"<div class='score'>{score} / {len(auto)}</div>")
        if len(auto) < len(marked):
            parts.append("<div class='muted'>Written answers will be marked by your teacher.</div>")
        parts.append("</div>")
    if quiz["show_answers"]:
        for i, (q, (text, result)) in enumerate(zip(quiz["questions"], marked)):
            if result is None:
                verdict, right = "<span class='muted'>Teacher will mark</span>", ""
            elif result:
                verdict, right = "<span class='ok'>✓ Correct</span>", ""
            else:
                correct = (q["options"][q["correct_option"]] if q["kind"] == "choice" and q["correct_option"] is not None
                           else (q["answers"][0] if q["answers"] else ""))
                verdict = "<span class='bad'>✗ Not quite</span>"
                right = f"<div>Correct answer: <strong>{esc(correct)}</strong></div>"
            parts.append(f"<div class='card'><div class='qnum'>Question {i + 1}</div>"
                         f"<div class='qtext'>{esc(q['text'])}</div>"
                         f"<div>Your answer: <strong>{esc(text) or '<em>(blank)</em>'}</strong></div>"
                         f"{right}<div>{verdict}</div></div>")
    return page("Submitted", "".join(parts))


def render_teacher_home(quizzes, student_url):
    rows = []
    for q in quizzes.values():
        _, results = read_results(q["id"])
        rows.append(f"<tr><td><a href='/teacher/{quote(q['id'])}'>{esc(q['title'])}</a></td>"
                    f"<td>{esc(q['id'])}.txt</td><td>{len(q['questions'])}</td><td>{len(results)}</td></tr>")
    table = ("<table><tr><th>Quiz</th><th>File</th><th>Questions</th><th>Responses</th></tr>"
             + "".join(rows) + "</table>") if rows else \
        "<p>No quizzes found. Put quiz <code>.txt</code> files in the <code>quizzes</code> folder.</p>"
    body = f"""<h1>Teacher view</h1>
<div class="card"><strong>Address for students:</strong>
<div class="score" style="font-size:1.6rem">{esc(student_url)}</div>
<div class="muted">Post this in your Teams class. It only works while this program is running and
students are on the school network.</div></div>
{table}
<p class="muted">Results are saved on this computer in:<br><code>{esc(RESULTS_DIR)}</code><br>
Open the <code>.csv</code> files with Excel. This page can only be opened on this computer.</p>"""
    return page("Teacher view", body, wide=True)


def render_teacher_quiz(quiz):
    header, results = read_results(quiz["id"])
    n_q = len(quiz["questions"])
    parts = [f"<p><a href='/teacher'>← All quizzes</a></p><h1>{esc(quiz['title'])}</h1>",
             f"<p>{len(results)} response(s). File: <code>{esc(results_path(quiz['id']))}</code></p>"]
    if not results:
        return page(quiz["title"], "".join(parts) + "<p class='muted'>No responses yet.</p>", wide=True)

    # per-question summary
    parts.append("<h2>Question summary</h2><div class='scroll'><table><tr><th>#</th><th>Question</th>"
                 "<th>% correct</th></tr>")
    for i, q in enumerate(quiz["questions"]):
        col = 5 + i * 2 + 1
        marks = [r[col] for r in results if len(r) > col]
        if q["kind"] == "written":
            pct = "teacher marks"
        else:
            pct = f"{round(100 * marks.count('1') / len(marks))}%" if marks else "-"
        parts.append(f"<tr><td>{i + 1}</td><td>{esc(q['text'])}</td><td>{pct}</td></tr>")
    parts.append("</table></div>")

    # per-student table
    parts.append("<h2>Students</h2><div class='scroll'><table><tr><th>Submitted</th><th>Name</th>"
                 "<th>Class</th><th>Score</th>" + "".join(f"<th>Q{i + 1}</th>" for i in range(n_q)) + "</tr>")
    for r in results:
        cells = []
        for i in range(n_q):
            a = 5 + i * 2
            ans, mk = (r[a], r[a + 1]) if len(r) > a + 1 else ("", "")
            ans = ans[1:] if ans.startswith("'") else ans
            cls = "ok" if mk == "1" else ("bad" if mk == "0" else "")
            cells.append(f"<td class='{cls}'>{esc(ans)}</td>")
        name = r[1][1:] if r[1].startswith("'") else r[1]
        parts.append(f"<tr><td>{esc(r[0])}</td><td>{esc(name)}</td><td>{esc(r[2])}</td>"
                     f"<td>{esc(r[3])}/{esc(r[4])}</td>{''.join(cells)}</tr>")
    parts.append("</table></div>")
    return page(quiz["title"], "".join(parts), wide=True)


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

    def send_html(self, content, status=200):
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def not_found(self):
        self.send_html(page("Not found", "<h1>Page not found</h1><p><a href='/'>Back to quizzes</a></p>"), 404)

    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])
        quizzes = list_quizzes()

        if path == "/":
            return self.send_html(render_home(quizzes))
        m = re.fullmatch(r"/quiz/([^/]+)", path)
        if m:
            quiz = quizzes.get(m.group(1))
            return self.send_html(render_quiz(quiz)) if quiz else self.not_found()

        if path.startswith("/teacher"):
            if not self.is_local():
                return self.send_html(page("Teacher only", "<h1>Teacher only</h1>"
                                           "<p>This page can only be opened on the teacher's computer.</p>"), 403)
            if path in ("/teacher", "/teacher/"):
                return self.send_html(render_teacher_home(quizzes, self.student_url))
            m = re.fullmatch(r"/teacher/([^/]+)", path)
            if m and m.group(1) in quizzes:
                return self.send_html(render_teacher_quiz(quizzes[m.group(1)]))
        return self.not_found()

    def do_POST(self):
        path = unquote(self.path.split("?", 1)[0])
        m = re.fullmatch(r"/quiz/([^/]+)/submit", path)
        quiz = list_quizzes().get(m.group(1)) if m else None
        if not quiz:
            return self.not_found()

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self.send_html(page("Error", "<h1>Submission too large</h1>"), 413)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        answers = {k: v[0] for k, v in form.items()}

        name = " ".join(answers.get("name", "").split())[:MAX_NAME_LEN]
        group = " ".join(answers.get("group", "").split())[:40]
        if not name:
            return self.send_html(render_quiz(quiz, "Please type your name."), 400)

        marked = mark(quiz, answers)
        save_result(quiz, name, group, marked)
        auto = [r for _, r in marked if r is not None]
        print(f"  {datetime.now():%H:%M:%S}  {quiz['title']}: {name} "
              f"({sum(1 for r in auto if r)}/{len(auto)})")
        self.send_html(render_submitted(quiz, name, marked))


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
    args = parser.parse_args()

    os.makedirs(QUIZ_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    Handler.student_url = f"http://{local_ip()}:{args.port}"
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)

    print("=" * 60)
    print(" Classroom Quiz Server is running")
    print("=" * 60)
    print(f" Students go to:   {Handler.student_url}")
    print(f" Teacher view:     http://localhost:{args.port}/teacher")
    print(f" Results saved in: {RESULTS_DIR}")
    print(f" Quizzes loaded:   {len(list_quizzes())}")
    print(" Close this window (or press Ctrl+C) to stop.")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
