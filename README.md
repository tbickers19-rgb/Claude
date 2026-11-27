# Classroom Quiz Server

A quiz website that runs on **your own computer**. Students open it in their
browser at school, answer the questions, and their answers are saved to a
spreadsheet file **on your computer only**.

- Student data is **never** sent to Claude, the internet, or any other service.
- You use Claude only to *write the questions*. You don't share student information with it.
- Results are saved as `.csv` files that open in Excel.
- Quizzes are marked automatically. Written answers are left for you to mark.

---

## 1. One-time setup

### Mac
1. Download this folder (on GitHub: **Code → Download ZIP**). Double-click the
   ZIP in Downloads to unzip it, and move the folder to e.g. `Documents/Quiz`.
2. **Right-click** `start-quiz-server.command` and choose **Open**, then click
   **Open** again in the warning. You only need to do this the first time;
   after that a double-click works. (macOS warns about any script downloaded
   from the internet.)
3. A Terminal window opens. If the Mac has no Python, it downloads a private copy
   into the quiz folder. This takes a minute, needs no admin password, and
   isn't installed anywhere else on the Mac. Then the quiz starts.
4. If macOS asks *"Do you want python3 to accept incoming network
   connections?"* click **Allow**.

The Mac is kept awake while the window is open. To stop the quiz, close the
Terminal window.

### Windows
1. Download and extract the folder as above.
2. Double-click **`start-quiz-server.bat`**. It installs Python if needed, then starts.

## 2. Running a quiz in class

1. Double-click **`start-quiz-server.command`** (Mac) or **`start-quiz-server.bat`** (Windows).
   A window opens and shows:
   ```
   Students go to:   http://10.x.x.x:8000
   Teacher view:     http://localhost:8000/teacher
   ```
2. The first time you run it, your computer may ask whether to allow incoming
   network connections. Click *Allow*.
3. **Post the "Students go to" address in your Teams class**, or open the
   teacher view and click **Show on the projector**: it shows the address
   in big letters with a QR code students can scan. Students open it,
   type or pick their name, do the quiz and submit.
4. Open **http://localhost:8000/teacher** on your laptop to watch responses come in.
   It updates by itself. You'll see each student's score and the % correct for
   each question. This page **only opens on your laptop**. Students get "Teacher only".
5. When time's up, click **Close** next to the quiz. Students can't open it or
   send answers any more. Click **Open** to let them back in.
6. When you're finished, close the window. Results stay saved in the
   `results` folder.

> Your laptop must be on, with the window open, while students do the
> quiz. The address may change from day to day, so check it each lesson.

Students don't lose their answers if the page reloads, the tab closes or the
Wi-Fi drops: answers are kept on their device until they submit. Pressing
refresh or Submit twice doesn't send their answers twice.

### Marking in the teacher view
Click a quiz in the teacher view to see every answer. Each answer has a small
button: **✓** (right), **✗** (wrong) or **?** (not marked yet, shown in yellow).
Click it to change the mark. It saves straight away and the score updates.
Use this to mark written answers, or to accept an answer the computer marked wrong.

If you fix a wrong answer in a quiz file after students have sent theirs, the
quiz page tells you how many marks no longer match and offers **Re-mark using
the quiz file**. Re-marking keeps your marks on written answers, but replaces
marks you changed by hand on auto-marked questions.

> If the results file is open in Excel on Windows, marks can't be saved. Close
> Excel and try again. Students' answers aren't saved either while it's open, so
> only open the file after the lesson.

### Class lists (optional)
Instead of typing their names (and misspelling them), students can pick from a
list. Make a `classes` folder next to `quizzes`, and put one plain-text file per
class in it, named after the class, with one name per line:

```
classes/9SD.txt     Aroha Ngata
                    Ben Li
                    ...
```

Students choose their class, then their name. There's a "My name isn't here"
option if someone is missing. **Anyone who can open the quiz page can see the
names in these lists**, so only use first names and surnames, nothing else.
The `classes` folder is excluded from git.

### If students can't connect
Some school networks block devices from talking to each other. If students see
"can't reach this page":
- check your laptop and the students are on the same Wi-Fi network,
- check you clicked *Allow* on the firewall prompt,
- ask IT: *"Can students reach port 8000 on my laptop on the school network?
  It's a local quiz page; no data leaves my laptop."*

## 3. Making a new quiz with Claude

Each quiz is a plain text file in the `quizzes` folder. Paste this prompt into
Claude, changing the topic:

```
Write a maths quiz in this exact plain-text format. Output only the quiz.

title: <quiz title>
instructions: <one line of instructions for students>
show_answers: yes

Q: <question>
A: <correct answer>            (use | between answers that are all accepted, e.g. A: 0.5 | 1/2)

Q: <multiple-choice question>
- <wrong option>
* <correct option>             (* marks the correct one)
- <wrong option>

Q: <question needing a written explanation>   (no A: line = I mark it myself)

Rules: answers must be short (a number, fraction or word). Use ² ³ √ π × ÷ − 
symbols rather than LaTeX. 

Topic: Year 9 simplifying algebraic expressions, 10 questions, getting harder.
```

Then:
1. Check the questions and answers. Claude can make mistakes.
2. Paste the result into TextEdit. Choose **Format → Make Plain Text**, then
   save it into the `quizzes` folder as e.g. `y9-simplifying.txt` (choose UTF-8 if asked).
   On Windows, use Notepad.
3. Refresh the page. The new quiz appears straight away; you don't need to restart.

To hide a quiz from students, move its file out of the `quizzes` folder.

### How marking works
- Numbers are compared by value: `0.5`, `.5`, `1/2` are equal, `1 1/2` = `1.5`,
  `1 000` = `1000`, and `x = 4` counts as `4`.
- Units are optional: if the answer is `5`, then `5 cm`, `45°`, `50%` or `$5` are
  fine. If you write a unit in the answer (`A: 5 cm`), a different unit (`5 m`) is
  marked wrong.
- Algebra terms can be in any order: `2b + 3a` = `3a + 2b`, `(x+3)(x+2)` = `(x+2)(x+3)`.
  Students can type `x^2`, `2*a` and `sqrt(12)` for `x²`, `2a` and `√12`.
  Answers are **not** simplified or expanded for them: `a + a` is wrong for `2a`,
  and `2(x + 3)` is wrong for `2x + 6`. Use `|` if you want to accept both forms.
- Text answers ignore capital letters and extra spaces.
- `show_answers: no` hides the correct answers after submitting (students still see their score).
- Questions with no `A:` line show as **check** in the spreadsheet. Mark them in the teacher view.

### Shuffling
Add a line near the top of a quiz file to give each student a different order,
which makes copying harder:

```
shuffle: yes          (questions and multiple-choice options)
shuffle: questions    (questions only)
shuffle: options      (multiple-choice options only)
```

Leave it out for quizzes that get harder as they go. Results are always saved
in the quiz file's order, so Q1 in the spreadsheet is always the first question
in the file.

## 4. Your results

Each quiz has a results file, `<quiz-name>.csv`, in the `results` folder (or your
school OneDrive, see below). It holds one row per submission: time, name, class,
score, and each answer with its mark (1 = right, 0 = wrong, check = you mark).
"Out of" counts only marked questions, so it goes up as you mark written answers.
Open it in Excel. If you add or remove questions after students have answered,
the old results are kept in a separate file whose name includes `-before-<date>`.
Fixing a typo or an answer keeps the same file.

If you change a question's wording while students are doing the quiz, students
who submit afterwards are asked to check their answers and submit again (their
answers are still filled in).

To check the marking still works after changing `quiz_server.py`, run
`python3 test_marking.py` and `python3 test_server.py`.

### Saving results to your school OneDrive
Results can save straight into your **school** OneDrive, so they're backed up
and you can open them from any device. They go from your laptop to OneDrive
directly. Nothing passes through Claude or any other service.

1. On the laptop you use in class, check OneDrive is signed in with your
   **school** account. School laptops usually have this already.
2. In your school OneDrive, make a folder called exactly **`Quiz results`**.
3. Right-click it and choose **Always keep on this device**, so the quiz can save
   there even if the Wi-Fi drops.
4. Start the quiz as usual. The window and the teacher view say
   `Results saved in your school OneDrive`.

The quiz never uses a personal OneDrive. If there's no `Quiz results` folder
in your school OneDrive, results are saved in the `results` folder as before.
On a Mac, the first time it saves there, macOS may ask whether python3 can
access files in OneDrive. Click **Allow**.

To use a different folder, make a file called `quiz-settings.txt` next to
`quiz_server.py` containing one line, for example:

```
results_folder: C:\Users\you\OneDrive - Your School\Year 9\Quiz results
```

If that folder doesn't exist, the quiz won't start, and tells you why, so
results never end up somewhere you didn't expect.

> Don't open a results file in Excel while students are answering. Look at
> the teacher view during the lesson and open the file afterwards. Otherwise
> OneDrive can make conflicting copies.

**Privacy tips**
- Don't paste the results files into Claude or any other online tool.
- Keep results in your school OneDrive or on your school laptop, not a personal
  account. Don't share the `Quiz results` folder or links to its files.
- Delete old results when your school's retention policy says to.
- The `results` folder, `classes` folder and `quiz-settings.txt` are excluded
  from git, so they won't be uploaded to GitHub.
