---
project: <project name from config.json>
ctx: 65536
edit: true
max_turns: 80
group: <task name, e.g. search-filters>
step: <1/3>
return: report
---

# <Task name>, step <n> of <m>: <what this step delivers>

Read `shared/overview.md` once for the project rules. This brief is complete: do not go looking
for a spec. Work one numbered part at a time, read only the file you are about to edit, run the
check after each part, and add one line to `artifacts/NOTES.md` per finished part.

## Why

<Two or three sentences: what the user wants and what is wrong today. If this is a correction,
say what you saw on screen and attach the screenshot with `llm put <id> name.png`.>

## Files (exactly these)

- `repo/<path>` - <create | edit>
- `repo/<path>` - <create | edit>

Do not touch anything else.

## Facts you need (so you do not open large files to find them)

- <type / function signature / field name, copied from the code>
- <for a file over ~800 lines: the exact anchor line each edit goes next to, and "do not read it whole">

## Part 1 - <file>

<Exact contract: exported names, types, numbered behaviour rules, edge cases. For UI: the element
tree with its classes and data-testids. Say what must NOT change.>

## Part 2 - <file>

...

## Tests

<File to create or update, and the cases by name. Include one case that fails on the obvious
wrong implementation.>

## Look at it (UI work only)

`screenshot` path `<url path>` scroll_to `<selector>`. Check: <three concrete things that must be
true in the picture>. Repeat with `theme: "light"`.

## Done condition

`format` every file you changed; `typecheck` PASSED; `test` passes for <files>. Summary: one line
per part, what differs from this brief, and what you saw in each screenshot.

<!--
Rules of thumb (delete this block before submitting):
- One layer per job, in dependency order: pure logic -> hook -> UI -> wiring. A step that needs
  three others' details is too big.
- A local model follows the brief literally, including its mistakes. Decide; do not offer options.
- It verifies only what has a check. Anything else (layout, colours, races) you review yourself.
- Mechanical bulk edits are a script, not a job. Async / state code gets a human review.
- `llm submit` prints warnings for missing pieces; `llm lint file.md` checks without submitting.
-->
