"""حزمة services — منطق العمل النقي (بلا Streamlit، بلا SQL مباشر).

تُترك فارغة من إعادة التصدير عمداً: كل خدمة تُستورد بمسارها الصريح
(مثل ``from services.matching_service import MatchingService``) لتفادي
تحميل تبعيات ثقيلة (rapidfuzz/engines) عند استيراد خدمة لا تحتاجها.
"""
