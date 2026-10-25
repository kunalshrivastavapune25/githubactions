Here are two prompts for Microsoft 365 Copilot. The first reads your workflow files and writes a slide-by-slide outline; the second turns that outline into a deck. Fill in the [brackets] first.

**How to use them**
1. **Copilot Chat:** attach the workflow file(s), paste Prompt 1, and answer its questions. If Copilot won't take `.yml` files, rename them to `.txt`.
2. **Word:** paste the outline, make each "Slide N – …" line a Heading 1, and save it to OneDrive.
3. **PowerPoint:** open Copilot › Create a new presentation from file › choose that Word file, and give it Prompt 2.
4. **Diagrams and screenshots:** select each numbered list of steps › Home › Convert to SmartArt › choose a Process layout. Then paste your screenshots into the labelled boxes.

**Prompt 1 – Copilot Chat, with the workflow files attached**

```
You are helping me prepare a short presentation for a client about our GitHub Actions deployment pipeline(s). The workflow file(s) are attached.

Audience: [client stakeholders, mostly non-technical, plus an AWS specialist]
Goal: [e.g. explain how this pipeline works and why it is safe — or — compare these pipelines and recommend one]
Length: about [8] slides, short and simple.

Step 1 – Read the files carefully. For each workflow, work out:
- how it is started, its inputs and their defaults
- every job and step, in order, including the conditions that decide whether each one runs
- which AWS services it reads or changes
- its safety checks: approvals, previews, drift checks, account checks, one-run-at-a-time rules, rollback
- what it shows in the run summary
Use only what the files actually contain. If a comment promises something that no job or step implements, point it out. Check that what each step reads is what earlier steps wrote. If anything is unclear, ask me rather than guessing.

Step 2 – Write a slide-by-slide outline. For every slide use exactly this format:
Slide N – [title, up to 8 words]
Key message: [one sentence]
On the slide: [3–5 short bullets in plain English; explain any technical term in brackets]
Diagram: [only if it helps – numbered steps, up to 6, each "Step – what happens", plus any branches, e.g. "Apply = no → stops after the report"]
SCREENSHOT: [only where a real screenshot proves the point – exactly what to capture and where, e.g. "GitHub › Actions › <workflow> › a test run › the job graph"]
Speaker notes: [2–4 sentences I can say]
Source: [the job and step names that back this slide]

Suggested order: 1 Title · 2 The problem, with an everyday analogy · 3 How it fits together (architecture) · 4 How a run works (flow, including where it stops and what happens on failure) · 5–6 Key features, one per slide · 7 What it is good for and what it doesn't do · 8 Recommendation or next steps (three clear asks or ideas).
If I am comparing pipelines, add a side-by-side table slide (criteria as rows, pipelines as columns, ✓ / partly / ✗ with a short reason) and a "where each one fits" slide.

Style:
- Plain English, short sentences, no more than about 40 words per slide.
- Be fair to other teams' work: say what it does well before its gaps.
- Never invent numbers, features or results.

End with two lists: "Questions for me" and "Claims to double-check before presenting".
```

**Prompt 2 – Copilot in PowerPoint, creating from the Word outline**
```
Create a presentation from this file. Make one slide per "Slide N" heading and keep the titles exactly as written. Put the "On the slide" bullets on the slide and the "Speaker notes" in the notes. Where there is a "Diagram", show the steps as a numbered list on the slide. Where there is a "SCREENSHOT" line, leave space on the right with a text box containing that exact line. Leave out the "Source" lines and the two lists at the end. Use a clean, simple design with one accent colour and no stock photos.
```

Prompt 1 is about 2,400 characters, which should be under Copilot's prompt limit. That limit is reported at around 8,000 characters for some accounts, but it varies. Expect a plainer design than these decks. Copilot in PowerPoint isn't documented as drawing flow diagrams, so the SmartArt step is how you get them. I've also saved both prompts to your GithubActions project as `claude/copilot-pipeline-deck-prompt.md`.

Sources:
- [DataCamp: Copilot in PowerPoint](https://www.datacamp.com/tutorial/copilot-in-powerpoint)
- [Fast.io: Microsoft Copilot character limit](https://fast.io/resources/copilot-character-limit/)
- [Microsoft Q&A: Copilot prompt character limits by license](https://learn.microsoft.com/en-us/answers/questions/2006445/do-prompt-character-limits-change-based-on-your-m3)