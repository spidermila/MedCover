# Contributing

## Principles

- Keep pull requests small enough to review easily. Smaller, cohesive changes
  are better. Keep the description concise: explain the problem, the resulting
  behavior, and how you verified it.
- If a larger PR is justified, discuss the design and agree on the intended
  behavior before implementation starts. Understanding the intentions makes
  implementation and review more straightforward.
- The author is responsible for the change and understands it, whether the
  code was written by them or with AI assistance. The author can explain the
  important parts, decisions, and known limitations. If something is unclear,
  take the time to understand it; simply relaying AI answers is not enough.

## Review gates

### 1. The author understands the change

The author can explain what changed, why, and how it works in their own words.
They can answer implementation questions without needing to ask AI during the
review and relay its answers. They understand AI-generated code too.

### 2. Automated checks pass

All relevant tests, linters, type checks, and required coverage checks pass.
Use the commands documented in the repository's README, CI, or configuration.
Any failure must be fixed or explained and explicitly accepted by a maintainer;
a green result does not replace behavioral review.

### 3. CodeRabbit review is resolved

Respond to every CodeRabbit comment. Make the suggested change when the
finding is valid. If no code change is warranted, explain why and what you
verified. Replies should help other reviewers and maintainers understand the
decision. Use fixups commits if it helps review individual fixes.

### 4. A second developer runs an AI review

A second developer runs an independent AI review of the current diff. The
author reads the output, understands each finding, and agrees with its wording
before passing it on. Forwarding AI output without assessing it is not a
review.

### 5. A human developer reviews the change

After automated and AI comments are resolved, a person reviews the change.
This gate may be skipped for small, low-risk changes, but a second pair of eyes
benefits the project. The reviewer checks behavior, tests, side effects, and
clarity. Findings must be fixed or closed with a clear explanation.

## Ready to merge

A PR is ready for approval when the applicable gates are complete, checks are
green, and every open comment has a clear resolution. Then approve and merge.
