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
3. **Post the "Students go to" address in your Teams class.** Students open it,
   type their name, do the quiz and submit.
4. Open **http://localhost:8000/teacher** on your laptop to watch responses come in.
   You'll see each student's score and the % correct for each question.
   This page **only opens on your laptop**. Students get "Teacher only".
5. When you're finished, close the window. Results stay saved in the
   `results` folder.

> Your laptop must be on, with the window open, while students do the
> quiz. The address may change from day to day, so check it each lesson.

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
- Numbers are compared by value: `0.5`, `.5` and `1/2` are equal, and `x = 4` counts as `4`.
- Text answers ignore capital letters and extra spaces.
- `show_answers: no` hides the correct answers after submitting (students still see their score).
- Questions with no `A:` line show as **check** in the spreadsheet for you to mark.

## 4. Your results

`results/<quiz-name>.csv` holds one row per submission: time, name, class,
score, and each answer with its mark (1 = right, 0 = wrong, check = you mark).
Open it in Excel. If you edit a quiz after students have answered, the old
results are kept in a separate file whose name includes `-before-<date>`.

**Privacy tips**
- Don't paste the results files into Claude or any other online tool.
- Keep the folder in a place only you can access. Delete old results when
  your school's retention policy says to.
- The `results` folder is excluded from git, so it won't be uploaded if this
  folder is synced to GitHub.
