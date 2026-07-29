# Guidance when generating quiz questions

- Use at least one distractor based on a common misconception the learner might have.
- Avoid yes/no questions — prefer multiple-choice with 4 distinct options.
- Prefer questions that ask about code *output* or *behavior* over questions that ask the learner to define terms.
- Make distractors *plausible*: wrong in a believable way, not obviously absurd.
- When testing a method, vary which method and which argument — don't lean on the same canonical example every time.
- Never pick option A just because it's first. The `generate_quiz_question` tool handles position randomization for you — always call it for every question.
