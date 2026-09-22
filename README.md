# Profit First

منصة قرار سيولة/تخصيص أموال: تحسب السيولة المتاحة فعليًا، وتوزّعها بالأولوية على الالتزامات، والمراجعة البشرية إلزامية قبل أي قرار. الخطة الكاملة في [plan.md](plan.md).

## التشغيل محليًا (SQLite، بدون أي إعداد قاعدة بيانات)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python scripts/generate_secrets.py      # يطبع الأسرار — الصقها في .env
uvicorn app.main:app --reload
```

افتح `http://127.0.0.1:8000`. للنشر على الإنترنت مجانًا (PostgreSQL + دومين HTTPS): [DEPLOY.md](DEPLOY.md).

## الاختبارات

```bash
pip install -r requirements-dev.txt
python -m pytest                                   # على SQLite
TEST_DATABASE_URL=postgresql://user:pass@host/db python -m pytest   # نفس الاختبارات على PostgreSQL
pip-audit -r requirements.txt                      # فحص ثغرات المكتبات
```

CI (GitHub Actions) يشغّل الثلاثة تلقائيًا عند كل push.

## البنية

- `app/engine/` — منطق الحساب (السيولة + التخصيص بالأولوية)، بدون اعتماد على أي framework. `xlsx_template.py` يقرأ "قالب تجميع البيانات" ديناميكيًا ويولّد قالبًا فارغًا.
- `app/api/` — JSON API محمي بـ`X-API-Key`.
- `app/ui/` — الواجهة (Jinja2، بدون أدوات بناء): الدخول، الهرمية، إدخال (Excel أو يدوي)، النتيجة والمراجعة، سجل كل العمليات، الرؤية المجمّعة.
- `app/db.py` — طبقة التخزين (SQLAlchemy): SQLite محليًا، PostgreSQL في الإنتاج عبر `DATABASE_URL`. كل عملية حساب تُحفظ ولا تُحذف، وأول قرار مراجعة نهائي.
- `app/config.py` — وضع الإنتاج (`APP_ENV=production`) يرفض التشغيل بإعداد ضعيف ويخفي `/docs`.
- `app/policy/allocation_policy.json` — البنود وأولوياتها وحمايتها.

## الأمان (ملخص)

استعلامات معاملة فقط · كلمات المرور bcrypt · كوكي موقّع `HttpOnly`/`Secure`/`SameSite` · حد محاولات الدخول · فحص Origin للنماذج · رؤوس CSP/HSTS/`X-Frame-Options` · بلا JavaScript مضمّن · حدود على رفع الملفات وحماية من zip-bomb · تحقق صارم من كل مدخل مالي · مراجعة بشرية append-only · فحص ثغرات المكتبات في CI.
