# Codex Prompts

Reusable prompts for working on this project.

## CHECK ONLY Audit

```text
CHECK ONLY. DO NOT MODIFY FILES.

Goal:
<describe the audit goal>

Analyze:
- bot.py
- admin_app.py
- db_schema.py
- shop_settings.py
- config.json
- products.json
- categories.json

Need report:
1. Current behavior.
2. Exact files/functions involved.
3. Risks.
4. Minimal safe patch plan.
5. Estimated patch size.

Rules:
- DO NOT MODIFY FILES.
- AUDIT ONLY.
- Be concrete.
```

## SURGICAL PATCH ONLY

```text
SURGICAL PATCH ONLY.

EDIT ONLY:
- <file list>

STRICT RULES:
- Do NOT touch unrelated files.
- Do NOT refactor unrelated code.
- Do NOT change business logic unless explicitly requested.
- Keep patch under <N> changed lines.

Goal:
<describe goal>

Tasks:
1. <task>
2. <task>

Validation:
python -m py_compile <changed python files>

Report:
- files changed
- functions changed
- SQL added/changed if any
- line delta
- preserved logic
```

Note:
After `python -m py_compile`, check `git status`. If tracked `__pycache__` files changed, restore those generated cache changes before final report unless the user explicitly asked to keep them.

## DOCUMENTATION ONLY PATCH

```text
SURGICAL PATCH ONLY. DOCUMENTATION ONLY.

EDIT ONLY:
- <markdown files>

STRICT RULES:
- Do NOT touch Python files.
- Do NOT touch config.json.
- Do NOT touch products.json/categories.json.
- Documentation only.

Goal:
<describe documentation update>

Tasks:
1. <task>
2. <task>

Validation:
Run only git diff review.

Report:
- docs updated
- key sections added
- no code/config/catalog files modified
```

## VALIDATION ONLY

```text
VALIDATION ONLY. DO NOT MODIFY FILES.

Goal:
Validate current project state.

Run:
- <specific validation commands>

Rules:
- Do NOT modify source files.
- If validation generates tracked __pycache__ changes, restore those generated cache files.
- Report commands run and results.
```

## STRICT READ ONLY FILE AUDIT

```text
CHECK ONLY. DO NOT MODIFY FILES.

STRICT READ ONLY FILE AUDIT.

Analyze only:
- <exact file list>

Need report:
1. Whether files exist.
2. What each file currently contains/does.
3. Missing or outdated information.
4. Risks.
5. Recommended changes, but do not implement them.

Rules:
- Do NOT modify files.
- Do NOT create files.
- Do NOT run formatting or validation commands that write files.
- AUDIT ONLY.
```

## PYCOMPILE CACHE CLEANUP NOTE

```text
When running:

python -m py_compile <file.py>

Then check:

git status --short

If tracked __pycache__ files changed, restore only those generated artifacts:

git restore -- __pycache__/<file>.pyc

Do not restore source files unless the user explicitly asks.
```

## NEW SHOP CLONE PLAN

```text
CHECK ONLY. DO NOT MODIFY FILES.

NEW SHOP CLONE PLAN.

Goal:
Prepare a practical clone/rebrand plan from the current project to:
<shop name>

Assumptions:
- Currency:
- Payment flow:
- Railway:
- PostgreSQL:
- Telegram bot:
- Telegram channel:
- Demo/MVP or production:

Analyze:
- config.json
- products.json
- categories.json
- shop_settings.py
- bot.py
- admin_app.py
- db_schema.py

Need report:
1. Exact files to change.
2. Exact env variables needed.
3. Telegram setup.
4. What can stay unchanged.
5. What still requires code edits.
6. Product/category plan.
7. Image plan.
8. Payment setup.
9. Clean DB startup.
10. Step-by-step checklist.
11. Estimated time.
12. Risks.
13. Test checklist.
```

## REBRAND TO NEW SHOP

```text
SURGICAL PATCH ONLY.

EDIT ONLY:
- config.json
- products.json
- categories.json

STRICT RULES:
- Do NOT touch Python files.
- Do NOT change DB schema.
- Keep valid UTF-8 JSON.
- Use image_url.
- Use is_active.

Goal:
Rebrand demo catalog/settings to <shop name>.

Config:
- brand_name:
- admin_panel_title:
- support_username:
- currency:
- currency_symbol:
- admin_id:
- iban:
- receiver_name:
- paypal:

Categories:
<list categories>

Products:
<list products with category_id, description, price_per_kg>

Validation:
python -m json.tool config.json
python -m json.tool products.json
python -m json.tool categories.json

Report:
- config changed
- categories changed
- products changed
- preserved logic
```

## DEPLOYMENT CHECK

```text
CHECK ONLY. DO NOT MODIFY FILES.

DEPLOYMENT CHECK.

Goal:
Verify this project is ready to deploy on Railway for <shop name>.

Analyze:
- bot.py
- admin_app.py
- db_schema.py
- shop_settings.py
- config.json
- requirements.txt

Need report:
1. Required services.
2. Required env variables.
3. Startup commands.
4. Database initialization order.
5. Telegram setup.
6. Channel setup.
7. Risks.
8. Launch checklist.
9. Test checklist.

Rules:
- AUDIT ONLY.
```

## BUG AUDIT

```text
CHECK ONLY. DO NOT MODIFY FILES.

BUG AUDIT.

Goal:
Find why <bug description>.

Analyze:
- relevant files/functions/routes/handlers

Need report:
1. Current behavior.
2. Expected behavior.
3. Exact code locations.
4. Likely root cause.
5. Safest patch plan.
6. Risks.
7. Validation plan.

Rules:
- DO NOT MODIFY FILES.
- AUDIT ONLY.
```
