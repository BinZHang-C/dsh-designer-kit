# dsh-designer-kit

An event-sourced project workbench for design teams running inside DeepSeek Harness (DSH), plus 12 interior-design skills.

It does not render images. It answers a different question: **who changed which number, on what evidence, and which downstream deliverables just became invalid.**

## Layers

- **Event log** — `workbench/state/events/<project>.jsonl`, append-only, every record carries `hash_prev` and a content hash. This is the only source of truth.
- **Projections** — condition table, deliverable states, three ledgers (changes / commitments / actions), canvas, retrospectives. Safe to discard and rebuild.
- **Interaction** — a human does exactly two things: confirm a fact, make a decision.

Every event must carry four elements or it is rejected: `subject` (an id'd entity), `actor` (person, or agent with `on_behalf_of`), `evidence` (file:line, mail id, meeting timestamp, review sheet number), `effect`.

## Install

```sh
./install.sh --skills          # copy 12 skills into ~/.dsh/skills (backs up existing)
export DESIGNER_KIT_HOME="$PWD"
dsh plugin add "$PWD/plugin"   # 5 tools in DSH
```

Tools: `designer_kit_today`, `designer_kit_events`, `designer_kit_proposals`, `designer_kit_record`, `designer_kit_verify`.

## Try it

```sh
cd workbench
python3 bin/eventlog.py bootstrap --project demo
python3 bin/eventlog.py set S-200.clear 2850mm --project demo --evidence "structural review / p2"
python3 bin/trigger.py list --project demo
python3 bin/eventlog.py verify --project demo
```

Changing one condition propagates: 6 deliverables invalidated across 3 hops, with the reason chain kept.

## Hard rules

- Drafters never write the log; a human confirms each candidate, with `file:line`.
- Deletes and overwrites are consent-gated and land in the commitment ledger; `rewind` appends a revert event instead of erasing history.
- Silence is not consent: proposals expire in 72h.
- Do not invent missing premises. The first parser version invented prerequisites for all 9 items and blocked every one; that was reverted.
- Editing history is detectable: `verify` recomputes the hash chain.

## Limits

Single machine, single user — no multi-tenant permission layer. Only the 11 self-contained design skills plus the workbench skill ship here; 18 others depend on an internal standards library and are not included ([docs/COVERAGE.md](docs/COVERAGE.md)). Tested on macOS + Python 3.9+; Linux unverified.

MIT licensed. Code-compliance hints in the skills do not replace a licensed engineer's sign-off.
