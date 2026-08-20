"""
بسته جمع‌آوری‌کننده‌های داده برای برندهای مختلف
"""

from .toshiba import collect_toshiba
from .hp import collect_hp
from .canon import collect_canon
from .brother import collect_brother
from .base import _counters_event, detect_brand, si, ss, validate_counter_consistency

__all__ = [
    'collect_toshiba',
    'collect_hp',
    'collect_canon',
    'collect_brother',
    '_counters_event',
    'detect_brand',
    'si',
    'ss',
    'validate_counter_consistency',
]