# Classroom Quiz Server

A quiz website that runs on **your own computer**. Students open it in their
browser at school, answer the questions, and their answers are saved to a
spreadsheet file **on your computer only**.

- Student data is **never** sent to Claude, the internet, or any other service.
- You use Claude only to *write the questions*. You don't share student information with it.
- Results are saved as `.csv` files that open in Excel.
- Quizzes are marked automatically. Written answers are left for you to mark.

---

## 1. One-time setup (Windows school laptop)

1. Download this folder (on GitHub: **Code → Download ZIP**), then right-click
   the ZIP and choose **Extract All** into e.g. `Documents\Quiz`.
2. Double-click **`start-quiz-server.bat`**. If Python isn't installed, it
   installs it for you (no admin needed), then starts the quiz.
   If that fails, it tells you to install **Python 3.12** from the Microsoft
   Store. Do that, then double-click the file again.

## 2. Running a quiz in class

1. Double-click **`start-quiz-server.bat`**. A black window opens and shows:
   ```
   Students go to:   http://10.x.x.x:8000
   Teacher view:     http://localhost:8000/teacher
   ```
2. The first time you run it, Windows may ask whether to let Python through the
   firewall. Tick **Private networks** (or Domain) and click *Allow*.
3. **Post the "Students go to" address in your Teams class.** Students open it,
   type their name, do the quiz and submit.
4. Open **http://localhost:8000/teacher** on your laptop to watch responses come in.
   You'll see each student's score and the % correct for each question.
   This page **only opens on your laptop**. Students get "Teacher only".
5. When you're finished, close the black window. Results stay saved in the
   `results` folder.

> Your laptop must be on, with the black window open, while students do the
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
2. In Notepad, paste the result and save it into the `quizzes` folder as
   e.g. `y9-simplifying.txt`. In the Save dialog, choose *Encoding: UTF-8*.
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
