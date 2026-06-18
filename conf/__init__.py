"""حزمة conf — الثوابت الموحّدة والإعدادات (المصدر الوحيد للحقيقة).

سُمّيت ``conf`` لا ``config`` تفادياً للتصادم مع ``config.py`` الجذري القديم
الذي تستورده وحدات ``engines/`` و``utils/`` عبر ``from config import ...``.
"""
from conf.settings import Settings

__all__ = ["Settings"]
