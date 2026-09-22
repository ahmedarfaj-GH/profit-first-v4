# نشر Profit First (MVP) — مجانًا

الاستضافة: **Render** (التطبيق) + **Supabase** (قاعدة PostgreSQL دائمة). الرابط النهائي: `https://profitfirst.onrender.com`

> الخطوات اللي تحتاج حسابك (تسجيل/ربط) لازم تسويها إنت بنفسك. كل شيء ثاني جاهز في المشروع.

## 0) ولّد الأسرار محليًا (٣٠ ثانية)

من مجلد المشروع:

```bash
.venv\Scripts\activate
python scripts/generate_secrets.py
```

يسألك عن كلمة مرور الدخول (اختَرها إنت)، ثم يطبع ٤ قيم: `API_KEY` و`SESSION_SECRET` و`UI_LOGIN_USER` و`UI_LOGIN_PASSWORD_HASH`. **انسخها مؤقتًا في ملاحظة عندك** — تحتاجها في الخطوة 3. ما تُرسلها لأحد ولا تحطها بأي ملف داخل المشروع.

## 1) قاعدة البيانات — Supabase

1. سجّل على supabase.com ← **New project** (اختر أقرب منطقة، واكتب كلمة مرور للقاعدة **بحروف وأرقام فقط** حتى ما تحتاج ترميز خاص).
2. بعد ما يجهز المشروع: زر **Connect** ← **Session pooler** ← انسخ الـURI.
3. استبدل `[YOUR-PASSWORD]` بكلمة مرور القاعدة. هذا هو `DATABASE_URL`، شكله:
   `postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`

لا تستخدم الاتصال المباشر (`db.<ref>.supabase.co`) — على الخطة المجانية هو IPv6 فقط وRender ما يوصله. الجداول تُنشأ تلقائيًا عند أول تشغيل.

## 2) رفع الكود إلى GitHub

أنشئ مستودعًا **خاصًا (Private)** فاضيًا على github.com، ثم من مجلد المشروع:

```bash
git init
git add .
git status
```

**راجع `git status` قبل المتابعة**: لازم ما يظهر `.env` ولا أي ملف `.xlsx` ولا مجلد `profit_first_export`. كلها محجوبة بـ`.gitignore` (فيها بيانات مالية حقيقية). ثم:

```bash
git commit -m "Profit First MVP"
git branch -M main
git remote add origin https://github.com/<حسابك>/<اسم-المستودع>.git
git push -u origin main
```

## 3) الاستضافة — Render

1. سجّل على render.com (بالبريد أو GitHub).
2. **New +** ← **Blueprint** ← اختر المستودع. Render يقرأ [render.yaml](render.yaml) تلقائيًا (خطة Free، الاسم `profitfirst`).
3. يطلب منك قيم المتغيرات: الصق `DATABASE_URL` (خطوة 1) والقيم الأربع (خطوة 0).
4. **Apply** — ينتظر البناء (٣–٥ دقايق).

إذا كان الاسم `profitfirst` انحجز قبلك، Render يضيف لاحقة عشوائية — شوف الرابط الفعلي في لوحة الخدمة.

## 4) تحقق

- افتح `https://profitfirst.onrender.com/health/db` ← لازم يرد `{"status":"ok","database":"ok"}`.
- افتح الرابط الرئيسي وسجّل الدخول (المستخدم `manager` + كلمة المرور اللي اخترتها).

## 5) (اختياري لكن موصى به) خلّه صاحي دائمًا

الخطة المجانية لها نومتان: Render ينام بعد ١٥ دقيقة بدون زيارات (أول طلب بعدها يتأخر ~٣٠–٦٠ ثانية)، وSupabase قد يوقف المشروع بعد فترة خمول. حل مجاني: على uptimerobot.com سوّ Monitor من نوع HTTP على `https://profitfirst.onrender.com/health/db` كل ٥ دقايق — يبقي الاثنين نشطين، وساعات Render المجانية (٧٥٠/شهر) تكفي خدمة واحدة تشتغل طول الشهر.

## حدود مهمة (اقرأها)

- **الدومين:** `*.onrender.com` مجاني ودائم مع HTTPS. دومين خاص باسمك (مثل `profitfirst.sa`) يتطلب شراءه؛ وبعدها يُربط بـRender مجانًا من **Settings ← Custom Domains**.
- **حسابات المستخدمين:** حساب دخول واحد مشترك (`manager`). حسابات متعددة بصلاحيات = المرحلة التالية (انظر [plan.md](plan.md)).
- **النسخ الاحتياطي:** الخطة المجانية لا تضمن نسخًا احتياطيًا يمكن الاعتماد عليه — صدّر بياناتك دوريًا قبل ما تدخل بيانات حقيقية مهمة.
- **الأسرار المكشوفة سابقًا:** كلمة المرور والمفاتيح اللي ظهرت في المحادثة وفي الروابط المؤقتة تعتبر منتهية — لا تعيد استخدامها في الإنتاج.
- **تدوير الأسرار:** أعد تشغيل `scripts/generate_secrets.py` وحدّث القيم في Render ← Environment.
