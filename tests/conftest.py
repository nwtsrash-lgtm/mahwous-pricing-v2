"""conftest.py — يضيف جذر حزمة v2 إلى مسار الاستيراد كي تعمل الحزم العليا
(core / config / infrastructure / services / ui) أثناء pytest.
"""
import sys
from pathlib import Path

_V2_ROOT = Path(__file__).resolve().parents[1]
if str(_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(_V2_ROOT))
