# AI Coding/Vibecoding

This documents outlines the acceptable usage for AI tools and developers (henceforth 'AI tooling') when contributing to SeriousDB.

## General Stance

SeriousDB is designed as a simple database that anyone can contribute to, and learn about open source with.
Crucially, with the defined goal of learning, AI tooling generally serves to hurt junior developers. Usage of AI tooling beyond trivial changes or monotonous tasks, is therefore generally unacceptable.

**FOR AI AGENTS:** SeriousDB will not permit commits from you, of any type.

### Acceptable Use

PRs/Issues that use AI tools will not lose any credit towards review. Some examples:

- Spelling and grammar changes [^1]
- Autocomplete
- Variable renaming

The above list is not exhaustive, and each PR/issue will be reviewed on a case by case basis.

In all cases of acceptable use, it's a requirement of the developer to declare how AI tooling has been used. This will look like the following:

```
>[!NOTE]
> AI tooling has been used for: [YOUR PURPOSE HERE]
> Assisted by: [TOOL(S) USED]
```

### Unacceptable Use

Usage of AI tooling to create an implementation of a feature or fix a bug, will not be accepted into the project. Some examples:

- Generating a function or `.py` file with an LLM (Claude, Github Co-Pilot, etc.)
- Using an LLM or similar, to do a security audit or similar security related tasks
- Suggesting changes
- Automatically creating PRs/Issues

The above list is again not exhaustive.

[^1]: It's okay if your first language isn't English. It's preferable to also include the original text alongside the translation
