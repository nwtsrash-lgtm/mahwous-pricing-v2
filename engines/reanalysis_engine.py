"""
engines/reanalysis_engine.py — محرك إعادة التحليل الفردي v1.0
✅ إعادة تحليل منتج واحد من الصفر
✅ إعادة تمريره عبر كافة فلاتر المطابقة
✅ تحديث حالته تلقائياً في المكان الصحيح
✅ تسجيل كامل للعملية
"""

import logging
import pandas as pd
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum
from datetime import datetime

logger = logging.getLogger("ReanalysisEngine")


class ReanalysisStatus(Enum):
    """حالات إعادة التحليل"""
    PENDING = "⏳ قيد الانتظار"
    RUNNING = "🔄 جاري التحليل"
    COMPLETED = "✅ اكتمل"
    FAILED = "❌ فشل"


@dataclass
class ReanalysisResult:
    """نتيجة إعادة التحليل الواحدة"""
    product_id: str
    product_name: str
    old_decision: str
    new_decision: str
    old_match_score: float = 0.0
    new_match_score: float = 0.0
    status: ReanalysisStatus = ReanalysisStatus.PENDING
    timestamp: str = None
    reason: str = ""
    matched_with: Optional[str] = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()
    
    def to_dict(self):
        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "old_decision": self.old_decision,
            "new_decision": self.new_decision,
            "old_match_score": self.old_match_score,
            "new_match_score": self.new_match_score,
            "status": self.status.value,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "matched_with": self.matched_with,
        }


class ReanalysisEngine:
    """
    محرك إعادة التحليل الفردي
    يسمح بإعادة تحليل منتج واحد من الصفر وتحديث حالته تلقائياً
    """
    
    def __init__(self, match_engine=None, routing_engine=None):
        """
        Args:
            match_engine: محرك المطابقة
            routing_engine: محرك التوزيع
        """
        self.match_engine = match_engine
        self.routing_engine = routing_engine
        self.reanalysis_history: Dict[str, ReanalysisResult] = {}
        self.pending_reanalysis: Dict[str, Dict] = {}
    
    def queue_product_for_reanalysis(
        self,
        product_id: str,
        product_data: Dict,
        reason: str = ""
    ) -> bool:
        """
        إضافة منتج إلى قائمة الانتظار لإعادة التحليل
        
        Args:
            product_id: معرف المنتج
            product_data: بيانات المنتج الكاملة
            reason: السبب وراء إعادة التحليل
        
        Returns:
            True إذا نجحت العملية
        """
        try:
            self.pending_reanalysis[product_id] = {
                "data": product_data,
                "reason": reason,
                "queued_at": datetime.now().isoformat()
            }
            logger.info(f"📋 تم إضافة {product_id} إلى قائمة إعادة التحليل")
            return True
        except Exception as e:
            logger.error(f"❌ فشل إضافة المنتج إلى قائمة الانتظار: {str(e)}")
            return False
    
    def reanalyze_single_product(
        self,
        product_id: str,
        product_data: Dict,
        our_catalog: pd.DataFrame,
        old_decision: str = "",
        old_match_score: float = 0.0,
        match_threshold: float = 85.0,
        review_threshold: float = 70.0
    ) -> ReanalysisResult:
        """
        إعادة تحليل منتج واحد من الصفر
        
        Args:
            product_id: معرف المنتج
            product_data: بيانات المنتج
            our_catalog: الكتالوج الأساسي
            old_decision: القرار السابق
            old_match_score: نسبة المطابقة السابقة
            match_threshold: حد المطابقة
            review_threshold: حد المراجعة
        
        Returns:
            نتيجة إعادة التحليل
        """
        logger.info(f"🔄 جاري إعادة تحليل المنتج: {product_id}")
        
        product_name = self._extract_product_name(product_data)
        
        result = ReanalysisResult(
            product_id=product_id,
            product_name=product_name,
            old_decision=old_decision,
            new_decision="",
            old_match_score=old_match_score,
            status=ReanalysisStatus.RUNNING
        )
        
        try:
            # المرحلة 1: التحقق من صحة البيانات
            logger.debug("  📋 المرحلة 1: التحقق من صحة البيانات...")
            validation_issues = self._validate_product_data(product_data)
            if validation_issues:
                result.status = ReanalysisStatus.FAILED
                result.reason = f"فشل التحقق: {', '.join(validation_issues)}"
                logger.warning(f"  ⚠️ {result.reason}")
                return result
            
            # المرحلة 2: تطبيق الفلاتر الصارمة
            logger.debug("  🔍 المرحلة 2: تطبيق الفلاتر الصارمة...")
            is_valid, filter_issues = self._apply_strict_filters(product_data)
            if not is_valid:
                result.new_decision = "⚪ مستبعد (فشل الفلاتر)"
                result.reason = f"تم استبعاده: {', '.join(filter_issues)}"
                logger.info(f"  ⚪ {result.reason}")
                result.status = ReanalysisStatus.COMPLETED
                return result
            
            # المرحلة 3: البحث عن أفضل مطابقة
            logger.debug("  🔎 المرحلة 3: البحث عن أفضل مطابقة...")
            best_match, best_score = self._find_best_match_in_catalog(
                product_name,
                our_catalog
            )
            
            result.new_match_score = best_score
            
            # المرحلة 4: اتخاذ القرار
            logger.debug("  ⚖️ المرحلة 4: اتخاذ القرار...")
            if best_score >= match_threshold:
                result.new_decision = "✅ تم المطابقة"
                result.matched_with = best_match
                result.reason = f"نسبة مطابقة عالية: {best_score:.1f}%"
            elif best_score >= review_threshold:
                result.new_decision = "⚠️ تحت المراجعة"
                result.matched_with = best_match
                result.reason = f"نسبة مطابقة متوسطة: {best_score:.1f}% (تحتاج مراجعة يدوية)"
            else:
                result.new_decision = "🔍 منتج مفقود"
                result.reason = f"لا توجد مطابقة قوية (أفضل نسبة: {best_score:.1f}%)"
            
            result.status = ReanalysisStatus.COMPLETED
            
            # تسجيل النتيجة
            logger.info(
                f"✅ اكتمل التحليل: {product_id} → {result.new_decision} "
                f"(النسبة: {best_score:.1f}%)"
            )
            
            # حفظ في السجل
            self.reanalysis_history[product_id] = result
            
            # إزالة من قائمة الانتظار إن وجد
            if product_id in self.pending_reanalysis:
                del self.pending_reanalysis[product_id]
            
            return result
        
        except Exception as e:
            logger.error(f"❌ خطأ في إعادة التحليل: {str(e)}")
            result.status = ReanalysisStatus.FAILED
            result.reason = str(e)
            return result
    
    def reanalyze_batch(
        self,
        products: Dict[str, Dict],
        our_catalog: pd.DataFrame,
        match_threshold: float = 85.0,
        review_threshold: float = 70.0
    ) -> Dict[str, ReanalysisResult]:
        """
        إعادة تحليل مجموعة من المنتجات
        
        Args:
            products: قاموس {product_id: product_data}
            our_catalog: الكتالوج الأساسي
            match_threshold: حد المطابقة
            review_threshold: حد المراجعة
        
        Returns:
            قاموس بنتائج إعادة التحليل
        """
        logger.info(f"🚀 بدء إعادة تحليل {len(products)} منتج...")
        
        results = {}
        for product_id, product_data in products.items():
            result = self.reanalyze_single_product(
                product_id,
                product_data,
                our_catalog,
                match_threshold=match_threshold,
                review_threshold=review_threshold
            )
            results[product_id] = result
        
        logger.info(f"✅ انتهت إعادة التحليل: {len(results)} منتج")
        return results
    
    def reanalyze_from_queue(
        self,
        our_catalog: pd.DataFrame,
        match_threshold: float = 85.0,
        review_threshold: float = 70.0
    ) -> Dict[str, ReanalysisResult]:
        """
        معالجة جميع المنتجات في قائمة الانتظار
        
        Args:
            our_catalog: الكتالوج الأساسي
            match_threshold: حد المطابقة
            review_threshold: حد المراجعة
        
        Returns:
            قاموس بنتائج إعادة التحليل
        """
        if not self.pending_reanalysis:
            logger.info("📭 قائمة الانتظار فارغة")
            return {}
        
        logger.info(f"⏳ معالجة {len(self.pending_reanalysis)} منتج من قائمة الانتظار...")
        
        results = {}
        for product_id, item in list(self.pending_reanalysis.items()):
            result = self.reanalyze_single_product(
                product_id,
                item["data"],
                our_catalog,
                match_threshold=match_threshold,
                review_threshold=review_threshold
            )
            results[product_id] = result
        
        return results
    
    def get_reanalysis_summary(self) -> Dict[str, Any]:
        """الحصول على ملخص نتائج إعادة التحليل"""
        summary = {
            "total_reanalyzed": len(self.reanalysis_history),
            "successful": 0,
            "failed": 0,
            "pending": len(self.pending_reanalysis),
            "decisions_changed": 0,
            "details": []
        }
        
        for product_id, result in self.reanalysis_history.items():
            if result.status == ReanalysisStatus.COMPLETED:
                summary["successful"] += 1
            else:
                summary["failed"] += 1
            
            if result.old_decision != result.new_decision:
                summary["decisions_changed"] += 1
            
            summary["details"].append(result.to_dict())
        
        return summary
    
    def export_reanalysis_report(self, output_path: str) -> bool:
        """تصدير تقرير إعادة التحليل"""
        try:
            report_data = []
            for product_id, result in self.reanalysis_history.items():
                report_data.append(result.to_dict())
            
            if report_data:
                df = pd.DataFrame(report_data)
                df.to_excel(output_path, index=False, sheet_name='إعادة التحليل')
                logger.info(f"✅ تم تصدير التقرير إلى {output_path}")
                return True
            else:
                logger.warning("⚠️ لا توجد بيانات لتصديرها")
                return False
        except Exception as e:
            logger.error(f"❌ فشل التصدير: {str(e)}")
            return False
    
    # ─── دوال مساعدة خاصة ───
    
    def _extract_product_name(self, product_data: Dict) -> str:
        """استخراج اسم المنتج من البيانات"""
        possible_keys = [
            "منتج_المنافس", "المنتج", "اسم المنتج", "product_name",
            "product", "name", "Product", "Name"
        ]
        for key in possible_keys:
            if key in product_data and product_data[key]:
                return str(product_data[key]).strip()
        return "[بدون اسم]"
    
    def _validate_product_data(self, product_data: Dict) -> list:
        """التحقق من صحة بيانات المنتج"""
        issues = []
        
        # التحقق من وجود اسم المنتج
        product_name = self._extract_product_name(product_data)
        if not product_name or product_name == "[بدون اسم]":
            issues.append("اسم المنتج فارغ")
        
        # التحقق من وجود السعر (اختياري لكن مهم)
        price_keys = ["السعر", "سعر_المنافس", "price", "Price"]
        has_price = any(key in product_data and product_data[key] for key in price_keys)
        if not has_price:
            logger.warning("  ⚠️ لا يوجد سعر للمنتج")
        
        return issues
    
    def _apply_strict_filters(self, product_data: Dict) -> Tuple[bool, list]:
        """تطبيق الفلاتر الصارمة على المنتج"""
        issues = []
        
        product_name = self._extract_product_name(product_data)
        
        # فلتر 1: الكلمات المرفوضة (عينات، تقسيمات، إلخ)
        reject_keywords = [
            "sample", "عينة", "عينه", "decant", "تقسيم", "تقسيمة",
            "split", "miniature", "0.5ml", "1ml", "2ml", "3ml"
        ]
        if any(keyword.lower() in product_name.lower() for keyword in reject_keywords):
            issues.append("يحتوي على كلمات مرفوضة (عينة/تقسيم)")
            return False, issues
        
        # فلتر 2: الحجم (يجب أن يكون أكبر من 5 مل)
        import re
        ml_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:ml|مل|ملي)', product_name, re.I)
        if ml_match:
            ml = float(ml_match.group(1))
            if ml < 5:
                issues.append(f"الحجم صغير جداً ({ml} مل)")
                return False, issues
        
        return True, []
    
    def _find_best_match_in_catalog(
        self,
        product_name: str,
        catalog: pd.DataFrame
    ) -> Tuple[Optional[str], float]:
        """البحث عن أفضل مطابقة في الكتالوج"""
        if catalog.empty or not self.match_engine:
            return None, 0.0
        
        best_match = None
        best_score = 0.0
        
        # اكتشاف عمود الاسم
        possible_columns = [
            "منتج_المنافس", "المنتج", "اسم المنتج", "product_name",
            "product", "name", "Product", "Name"
        ]
        
        name_col = None
        for col in possible_columns:
            if col in catalog.columns:
                name_col = col
                break
        
        if not name_col:
            logger.warning("⚠️ لم يتم العثور على عمود الاسم في الكتالوج")
            return None, 0.0
        
        # البحث عن أفضل مطابقة
        for _, catalog_row in catalog.iterrows():
            catalog_name = str(catalog_row.get(name_col, "")).strip()
            if not catalog_name:
                continue
            
            # استخدام محرك المطابقة
            if hasattr(self.match_engine, 'calculate_match_score'):
                score = self.match_engine.calculate_match_score(
                    product_name,
                    catalog_name
                )
            else:
                # مطابقة بسيطة كبديل
                from rapidfuzz import fuzz
                score = fuzz.token_set_ratio(product_name, catalog_name)
            
            if score > best_score:
                best_score = score
                best_match = catalog_name
        
        return best_match, best_score
