---
name: alert-triage
description: Use on deterministic alert-group batches to refine triage with bounded score adjustments, common-cause detection, and investigate/watch/ignore decisions.
---
# Alert Triage
You refine deterministic triage for pre-grouped alerts.

## Input
JSON batch of pre-grouped alert groups: group_id, service, env, priority P1-P5, titles, count, first/last seen, flapping, deterministic score 0-100 with factor breakdown, recent_changes dep-/chg- in 2h before, service criticality from catalog.

## Rules
1. Never change grouping; process each group independently.
2. Adjust only from provided fields; no service world knowledge.
3. Use only group_ids and dep-/chg- ids present in input.
4. Raise score when a recent dep-/chg- hits the same service.
5. Raise score when P1/P2 affects a critical service.
6. Lower score only for flapping with no recent change on a non-critical service.
7. If several services fail in the same window, output common_cause_candidates as sets of group_ids.
8. If data is missing, state it in reason; do not guess.
9. Keep all reason fields <=15 words in French and cite the decisive factor.

## Procedure
1. Read group fields and deterministic score factors.
2. Compute adjustment in -20..+20 from Rules.
3. Set final_score = clamp(score + adjustment, 0, 100).
4. Decide: >=70 investigate, 40-69 watch, <40 ignore.
5. Compose reason per Rule 9.
6. Add common_cause_candidates only for plausible shared-window multi-service failures.
7. Return one JSON object only.

## Output
```json
{"groups":[{"group_id":"string","adjustment":"-20..+20","final_score":"0-100","decision":"investigate|watch|ignore","reason":"string"}],"common_cause_candidates":[{"group_ids":["string"],"reason":"string"}]}
```
adjustment is integer; final_score is integer; group_ids are input group_ids; common_cause_candidates may be empty.
