# Repository Instructions

## Question Data Guardrails

- Treat `data/*.csv` as the source of truth for question records. Regenerate `dist/bundles` from CSV instead of hand-editing generated bundles when possible.
- Before and after creating, importing, or editing question data, run `npm run exam:check`.
- If `exam` mismatches are reported, run `npm run exam:fix`, then rebuild generated artifacts:

```bash
npm run exam:fix
npm run build
npm run sync:ai-copy
node scripts/update_manifest.mjs --dist dist_with_ai
npm run exam:check
```

- Do not manually guess `exam` values when the script can infer them. The current canonical values are `combo_iroha`, `single_select`, `fill_blank`, and `calc_numeric`.
- Follow the consuming app's `QuestionExamPattern` semantics in `RealEstateAppraiser/composeApp/src/commonMain/kotlin/com/real/estate/appraiser/core/model/Question.kt`:
  - `combo_iroha`: questions with top-level `イ/ロ/ハ/ニ/ホ` statement groups whose choices are pure combinations such as `イとロ`, `イとロとハ`, or `イのみ`.
  - `single_select`: questions whose choices are ordinary sentence options and one option is selected.
  - `fill_blank`: fill-blank or cloze questions, including prompts that use `イ/ロ/ハ` labels for blanks.
  - `calc_numeric`: numeric calculation questions whose choices are numeric results such as yen, percent, or square-meter values.
