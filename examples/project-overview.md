# webapp - standing brief for coding jobs

Save a file like this as `projects/webapp/context/overview.md` in the queue repo. It is mounted
read-only into every job for the project as `shared/overview.md`, so write it for the model: short,
literal, and only what is true for every job. Anything specific to one task goes in that job's brief.

Mounted as `repo/`. You may edit only `repo/client/src/**`. React + TypeScript + Vite, tests with
Vitest.

## Rules that are checked automatically - break them and the work is rejected

1. TypeScript is strict. No `any`, no `// @ts-ignore`, no unused imports or variables.
2. Prettier style. Run the `format` check on each file you create or change.
3. Do not change server code, package.json, or anything outside `client/src`.
4. Preserve existing behaviour. Make focused changes; do not reformat code you were not asked
   to change.

## How to work

- `search` first, then `read_file` only the line range you need. `api.ts` is 3900 lines: never
  read it whole.
- New files: `write_file`. Existing files: `edit_file` with an exact, unique `old_string`.
- Checks (tool `check`): `typecheck` (no target), `test` (target = one test file), `format`
  (target = one file).
- Done means: `typecheck` PASSED and the tests you were asked for PASSED. Then summarise.

## Map

- `client/src/lib/api.ts` - every call to the server goes through here.
- `client/src/components/` - one folder per feature; tests live in `__tests__` next to the code.
