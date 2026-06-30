from datetime import datetime

class CTime:
    def __init__(self, year, month, day, hour=0, minute=0, second=0, auto=True):
        self.year = year
        self.month = month
        self.day = day
        self.hour = hour
        self.minute = minute
        self.second = second
        self.auto = auto
        self.set_timestamp()

    def __str__(self):
        if self.hour == 0 and self.minute == 0 and self.auto:
            return f"{self.year:04}/{self.month:02}/{self.day:02} {self.hour:02}:{self.minute:02}"
        else:
            return f"{self.year:04}/{self.month:02}/{self.day:02} {self.hour:02}:{self.minute:02}"

    def to_str(self):
        return str(self)

    def toDateStr(self, splt=''):
        return f"{self.year:04}{splt}{self.month:02}{splt}{self.day:02}"

    def toDate(self):
        return CTime(self.year, self.month, self.day, auto=False)

    def set_timestamp(self):
        self.datetime = datetime(self.year, self.month, self.day, self.hour, self.minute, self.second)
        self.ts = self.datetime.timestamp()

    def __gt__(self, other):
        return self.datetime > other.datetime

    def __ge__(self, other):
        return self.datetime >= other.datetime

    def __eq__(self, other):
        return self.datetime == other.datetime

    def __lt__(self, other):
        return self.datetime < other.datetime

    def __le__(self, other):
        return self.datetime <= other.datetime

class CChanException(Exception):
    def __init__(self, message, error_code):
        super().__init__(message)
        self.error_code = error_code

class ErrCode:
    KL_NOT_MONOTONOUS = "KL_NOT_MONOTONOUS"

def check_kline_time(kline_unit, klu_last_t):
    if not kline_unit.time > klu_last_t:
        raise CChanException(f"kline time err, cur={kline_unit.time}, last={klu_last_t}", ErrCode.KL_NOT_MONOTONOUS)
    return kline_unit.time