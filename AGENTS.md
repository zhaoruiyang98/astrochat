# Project instructions

## Design Decisions
- Ask when in doubt about any aspect of the implementation, however small.

## Code organization and readability

- Prioritize straightforward, readable execution over clever abstractions or efficiency. Some repetition or repeated computation is acceptable when it makes the logic easier to follow.
- Give each function one clear responsibility and aim to keep it within one editor screen. Split at meaningful boundaries; do not compress code or introduce trivial helpers merely to satisfy a line count.
- Pass data and settings explicitly through function arguments, constructor arguments, and return values. Avoid hidden shared state, closures that capture changing outer variables, and callbacks that capture a variable also being reassigned by the same expression. Make necessary framework state and lifecycle behavior explicit in nearby comments.
- Avoid defensive prechecks, especially when the subsequent operation would naturally raise an error. Prefer letting errors propagate with their original traceback over adding redundant validation or exception wrappers.
- Keep interfaces small. When input and output refer to the same reusable artifact, prefer one path argument with explicit behavior.

## Git commits

When creating commits:
- Keep the user's configured Git identity as the commit author.
- Append the following trailer to every commit created with Codex:

  Co-authored-by: Codex <codex@openai.com>
