# docs

What the system *is* and how we build it. How it came to be lives in git.

| File | Contents |
| --- | --- |
| [challenge.md](challenge.md) | The challenge as given: task, what 5U runs, the start kit, scoring, the final |
| [RULES.md](RULES.md) | Every rule we are scored or judged by, numbered, with source, place in code and status |
| [calc.md](calc.md) | The arithmetic of a price and how one option is chosen; where the open assumptions live |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | Open questions for 5U, how we win, the system, the backlog, the three work packages, the split by KPI |

Each agent documents itself next to its code:

| Agent | Contents |
| --- | --- |
| [router](../pixel/agents/router/README.md) | Which kind of mail this is |
| [rfq](../pixel/agents/rfq/README.md) | Request to quote, acceptance to booking |
| [tracking](../pixel/agents/tracking/README.md) | Tracking answers from the CRM |
| [tariffs](../pixel/agents/tariffs/README.md) | Tariff files and tariff mails into the store |

## Every agent README has the same sections

1. **Mission:** what the agent has to achieve, in two sentences
2. **Rules:** which rules (numbers from [RULES.md](RULES.md)) it covers
3. **Input / output:** what it receives, what it returns, which events it emits
4. **Implementation:** file, function, limits
5. **Pitfalls:** what goes wrong in the mail and how the agent handles it
6. **Open:** known gaps; done means delete

## Rules for these docs

- No values of a dataset version (rates, lanes, addresses). Describe observations in general terms.
- Link code by `file.py` + function name, never by line number.
- Change docs and code in the same commit. Delete what is no longer true.
- Keep it short. Don't retell what the code already says clearly.
- English only.
