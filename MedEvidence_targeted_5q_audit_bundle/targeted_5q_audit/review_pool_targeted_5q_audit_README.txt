MedEvidence targeted candidate audit - 5 queries

Files:
- review_pool_targeted_5q_assistant_audit.csv: 196 candidate rows for Q06, Q08, Q12, Q13, Q15, with provisional grades, reasons, quotes, and provenance fields.
- qrels_targeted_5q_assistant_provisional.tsv: four-column Qrels format for these five queries only.
- review_pool_targeted_5q_audit_diagnostics.json: counts and limitations.

Interpretation:
- Grades are AI-provisional and based only on text_excerpt from the candidate pool.
- Grade 3 means a direct answer to the precise question. No grade-3 candidate was found in the supplied excerpts.
- Grade 2 means partial evidence with a material population/setting limitation.
- Grade 1 means topical/background evidence that does not answer the requested decision.
- Grade 0 means no useful evidence for the exact question.
- Do not replace the earlier 15-query Qrels with this 5-query file.
- Before formal metrics or gold-standard claims, verify the candidate excerpts against full papers and conduct human relevance review.
