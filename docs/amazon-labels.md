# Amazon labels: first pilot

Build the pack locally from the repository root:

`PYTHONPATH=backend .venv/bin/python -m evals.build_amazon_labels`

The command reads the original files without modifying them and publishes a new directory at `backend/storage/amazon/labels-v1/`. It refuses to write inside the source or overwrite an existing output. Use a new `--out-dir` for later revisions. `manifest.json` fingerprints the raw product files, query definitions, generator and each output.

| File | Meaning |
| --- | --- |
| `catalog_facts.jsonl` | Product IDs, brands, category paths and USD reference prices copied from the source. Join on `product_id`. |
| `catalog_attributes.jsonl` | Pilot extraction for smartwatches, men's road-running shoes and coffee machines. Each field is `observed`, `ambiguous` or `unknown`. `observed` means a listing claim was found. |
| `catalog_qa.jsonl` | Questions and answers derived from observed fields and tied to listing evidence. These are catalog answers, separate from official FAQs and evaluation questions. |
| `retrieval_labels.jsonl` | English queries with authored criteria and binary labels derived from the catalog without calling the ranker. |
| `interaction_labels.jsonl` | Scripted intent, target selection, clarification and expected state after each turn. Catalog cases use frozen display order; checkout cases use the separate demo SKUs. |
| `demo_variants.jsonl` | Simulated combinations, USD prices and inventory. IDs start with `demo_`; a link to the original product is for identification only. |

Evidence uses zero-based character offsets with an exclusive end into the exact source field (`title` or `rag_knowledge.marketing_description`). Conflicting matches remain ambiguous. A missing value stays unknown. Listed colors and sizes describe the source text and do not establish valid SKU combinations. The pack does not provide real customer events, purchases or inventory.

For `exact_catalog_constraints`, a positive is any product meeting the stated leaf, brand and reference price criteria. `hard_negative_ids` records explicit neighboring categories such as watch bands. For `partially_observed_attributes`, `unjudged_ids` contains otherwise eligible products without enough evidence to decide. These cases can measure retrieval of the known positives; unknowns must stay out of false-positive counts, and recall over known positives cannot be described as exhaustive recall. Source taxonomy is a proxy for category membership and can itself contain errors.

This pack is a local pilot. Query criteria and expected turns are authored scenarios. A semantic benchmark still needs independently reviewed candidate relevance, difficult distractors, and a frozen held-out set. These sidecars leave current search behavior and checkout unchanged.
