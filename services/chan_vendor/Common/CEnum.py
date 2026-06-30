from enum import Enum, auto
from typing import Literal


class DATA_SRC(Enum):
    BAO_STOCK = auto()
    CCXT = auto()
    CSV = auto()


class KL_TYPE(Enum):
    K_1M = auto()
    K_DAY = auto()
    K_WEEK = auto()
    K_MON = auto()
    K_YEAR = auto()
    K_5M = auto()
    K_15M = auto()
    K_30M = auto()
    K_60M = auto()
    K_3M = auto()
    K_QUARTER = auto()


class KLINE_DIR(Enum):
    UP = auto()
    DOWN = auto()
    COMBINE = auto()
    INCLUDED = auto()


class FX_TYPE(Enum):
    BOTTOM = auto()
    TOP = auto()
    UNKNOWN = auto()


class BI_DIR(Enum):
    UP = auto()
    DOWN = auto()


class BI_TYPE(Enum):
    UNKNOWN = auto()
    STRICT = auto()
    SUB_VALUE = auto()  # 次高低点成笔
    TIAOKONG_THRED = auto()
    DAHENG = auto()
    TUIBI = auto()
    UNSTRICT = auto()
    TIAOKONG_VALUE = auto()


BSP_MAIN_TYPE = Literal['1', '2', '3']


class BSP_TYPE(Enum):
    T1o = '1o'
    T1D = '1d'  # 通过seg背驰+biout商最后一个bi背驰怕暖
    T1 = '1'
    T1P = '1p'
    T1PR = '1pr'
    T2 = '2'
    T2S = '2s'
    T3A = '3a'  # 中枢在1类后面
    T3B = '3b'  # 中枢在1类前面
    T3C = '3_left'  # 中枢在1类后面
    T3D = '3_right'  # 中枢在1类前面
    T3LC = '3_leftA'  # 中枢在1类后面
    T3LD = '3_leftB'  # 中枢在1类前面
    T3RC = '3_rightA'  # 中枢在1类后面
    T3RD = '3_rightB'  # 中枢在1类前面
    T4 = '4'

    def main_type(self) -> BSP_MAIN_TYPE:
        return self.value[0]  # type: ignore


class AUTYPE(Enum):
    QFQ = auto()
    HFQ = auto()
    NONE = auto()


class TREND_TYPE(Enum):
    MEAN = "mean"
    MAX = "max"
    MIN = "min"


class TREND_LINE_SIDE(Enum):
    INSIDE = auto()
    OUTSIDE = auto()


class LEFT_SEG_METHOD(Enum):
    ALL = auto()
    PEAK = auto()


class FX_CHECK_METHOD(Enum):
    STRICT = auto()
    LOSS = auto()
    HALF = auto()
    TOTALLY = auto()


class SEG_TYPE(Enum):
    BI = auto()
    SEG = auto()


class MACD_ALGO(Enum):
    AREA = auto()
    PEAK = auto()
    FULL_AREA = auto()
    DIFF = auto()
    SLOPE = auto()
    AMP = auto()
    VOLUMN = auto()
    AMOUNT = auto()
    VOLUMN_AVG = auto()
    AMOUNT_AVG = auto()
    TURNRATE_AVG = auto()
    RSI = auto()


class DATA_FIELD:
    FIELD_TIME = "time"
    FIELD_OPEN = "open"
    FIELD_HIGH = "high"
    FIELD_LOW = "low"
    FIELD_CLOSE = "close"
    FIELD_VOLUME = "volume"  # 成交量
    FIELD_TURNOVER = "turnover"  # 成交额
    FIELD_TURNRATE = "turnover_rate"  # 换手率


TRADE_INFO_LST = [DATA_FIELD.FIELD_VOLUME, DATA_FIELD.FIELD_TURNOVER, DATA_FIELD.FIELD_TURNRATE]
