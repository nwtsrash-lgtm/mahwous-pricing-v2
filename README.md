# مهووس — نظام التسعير الذكي v2

إعادة بناء التطبيق الأحادي (`app.py`، ‎8,587‎ سطراً) إلى **معمارية نظيفة** مع
**الحفاظ الحرفي** على منطق المطابقة والتصنيف وكشف المفقودات.

> **مبدأ التصميم الأول:** لا «تنظيف» يكسر السلوك. كل خوارزمية حرجة منقولة
> حرفياً من المصدر الحيّ (`app.py` / `engines/engine.py`) مع تعليقات
> `#PRESERVED_LOGIC` تشير إلى السطر الأصلي، ومُغطّاة باختبارات.

---

## ⚡ التشغيل

```bash
pip install -r mahwous-pricing-v2/requirements.txt
# من جذر المشروع (كي تُحَل engines/ و utils/ و config.py القديمة):
streamlit run mahwous-pricing-v2/app.py
```

## 🧪 الاختبارات

```bash
cd mahwous-pricing-v2
pytest -q          # 78 اختباراً (وحدة + تكامل)
```

---

## 🏛️ المعمارية (طبقات، الاعتماد للأسفل فقط)

```
┌──────────────────────────────────────────────────────────────┐
│  app.py  ── الموجّه فقط (128 سطراً، الوحيد المستورد لـ Streamlit)│
│            شريط جانبي → صفحة ، حاوية DI تحقن الخدمات            │
└───────────────┬──────────────────────────────────────────────┘
                │ يحقن
┌───────────────▼──────────────┐   ┌──────────────────────────┐
│  ui/ (طبقة العرض الرفيعة)     │   │  bootstrap.py (جذر التركيب)│
│  state_manager · components/ │   │  Container + run_analysis │
│  pages/ (أغلفة رقيقة)         │   └───────────┬──────────────┘
└───────────────┬──────────────┘               │ يبني
                │ يستدعي                         │
┌───────────────▼───────────────────────────────▼──────────────┐
│  services/ (منطق العمل النقي — بلا Streamlit، بلا SQL مباشر)   │
│  matching · classification · pricing · missing · audit        │
│  ai · export                                                   │
└───────────────┬───────────────────────────────┬──────────────┘
                │ يعيد استخدام (نواة محقونة)      │ يستخدم
┌───────────────▼──────────────┐   ┌─────────────▼──────────────┐
│  core/ (نماذج + تعداد + خطأ)  │   │  infrastructure/ db_manager │
│  Pydantic v2 · SectionType   │   │  (WAL، لا تعريف schema)     │
│  DataLossError               │   └────────────────────────────┘
└──────────────────────────────┘
                │ يستورد كسولاً (نواة نقية)
┌───────────────▼──────────────────────────────────────────────┐
│  engines/ · utils/ القديمة (تُغلَّف لا تُعاد كتابتها)            │
│  normalize_name · extract_size · CompetitorIntelligence …      │
└──────────────────────────────────────────────────────────────┘
```

**حقن الاعتماديات:** `bootstrap.build_container()` يبني الخدمات مرة واحدة؛
`app.py` يحقنها في الصفحات. لا حالة عامة — الحالة في `AppState` المُنمّط فوق
`st.session_state` (قابل للحقن بقاموس للاختبار).

---

## 🔒 المنطق المحفوظ (Preserved Logic)

| الخوارزمية | المصدر | الموقع الجديد |
|-----------|--------|---------------|
| `_miss_bare` + حجب الكلمات/الهيكل | app.py:624-660 | `services/matching_service.py` |
| حُرّاس الماركة/الحجم(8.0)/الجنس + ملكية 82/65 | app.py:898-982 | `services/matching_service.py` |
| `_split_results` (توزيع حصري + شبكة أمان) | app.py:392-513 | `services/classification_service.py` |
| القرار السعري (diff=سعرنا−المنافس) | engine.py:2337-2366 | `services/pricing_service.py` |
| `_compute_missing_from_store` + كاش F4v2 | app.py:726-1050 | `services/missing_service.py` |
| `_reconciliation_check` + فحص التكرار | app.py:516-614 | `services/audit_service.py` |
| تدوير AI + دفعات + كاش | ai_engine.call_ai | `services/ai_service.py` |
| حمولة Make + أعمدة سلة (40) | make_helper / salla_shamel | `services/export_service.py` |

### ⚖️ قانون حفظ البيانات
`run_analysis` يفرض: `len(all) == Σ(الأقسام)` **و** لا تكرار بين السعري والمفقود.
أي خرق ⇒ يرفع `DataLossError`. (اختبار `test_integration.py`.)

---

## 📌 قرارات هندسية وانحرافات مقصودة (شفافية)

1. **`config/` → `conf/`:** حزمة الإعدادات سُمّيت `conf` لتفادي تصادم قاتل مع
   `config.py` الجذري القديم الذي تستورده وتُعدّله كل وحدات `engines/`/`utils/`.
2. **ترتيب مزودات AI:** الحيّ هو **OpenRouter→Gemini→Cohere** (ليس Gemini أولاً)
   — حُفظ السلوك الفعلي (قابل للتهيئة)، لا المثالي.
3. **مُصدّر سلة:** يُغلَّف المُولّد القانوني (يستورد Streamlit) باستيراد كسول؛
   حمولة Make والأعمدة الأربعون منقولة أصلاً ومُختبَرة.
4. **إعادة الاستخدام لا التكرار:** المطبّعات (normalize_name…) والمصدر (DB schema)
   تُعاد استخدامها من الكود القديم عبر نوى محقونة — لا تُعاد كتابتها.

---

## 📁 البنية

```
mahwous-pricing-v2/
├── app.py                 # الموجّه (≤150)
├── bootstrap.py           # حاوية DI + run_analysis
├── conf/                  # constants · settings
├── core/                  # models · enums · exceptions
├── infrastructure/        # db_manager (WAL)
├── services/              # 7 خدمات نقية
├── ui/                    # state_manager · components/ · pages/
└── tests/                 # 78 اختباراً
```
