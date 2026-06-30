import os

from Common.CEnum import DATA_FIELD, KL_TYPE
from Common.ChanException import CChanException, ErrCode
from Common.CTime import CTime
from Common.func_util import str2float
from KLine.KLine_Unit import CKLine_Unit

from .CommonStockAPI import CCommonStockApi
import pandas as pd
from datetime import datetime


def convert_time_format(input_file, output_file, resampetime=None):
    df = pd.read_csv(input_file, parse_dates=['timestamp'])
    for col in ['close_time', 'quote_av', 'trades', 'tb_base_av', 'tb_quote_av', 'ignore']:
        if col in df.columns:
            del df[col]
    df = df[df['timestamp'] >= '2022-01-01 00:00:00']
    df['time'] = df['timestamp']
    del df['timestamp']

    if resampetime is None:
        df.set_index('time', inplace=True)
        df.reset_index(inplace=True)
        try:
            df['time'] = df['time'].dt.strftime('%Y-%m-%d %H:%M:%S.%f')
        except Exception:
            df['time'] = pd.to_datetime(df['time'])
            df['time'] = df['time'].dt.strftime('%Y-%m-%d %H:%M:%S.%f')
    else:
        df.set_index('time', inplace=True)
        df = df.resample(resampetime, label='right', closed='left').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna()
        df.reset_index(inplace=True)
        df['time'] = df['time'].dt.strftime('%Y-%m-%d %H:%M:%S.%f')

    def _convert(time_str):
        dt = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S.%f')
        return dt.strftime('%Y%m%d%H%M%S000')

    df['time'] = df['time'].apply(_convert)
    df.to_csv(output_file, index=False)
    return output_file


def create_item_dict(data, column_name):
    for i in range(len(data)):
        data[i] = parse_time_column(data[i]) if column_name[i] == DATA_FIELD.FIELD_TIME else str2float(data[i])
    return dict(zip(column_name, data))


def parse_time_column(inp):
    # 20210902113000000
    # 2021-09-13
    if len(inp) == 10:
        year = int(inp[:4])
        month = int(inp[5:7])
        day = int(inp[8:10])
        hour = minute = 0
    elif len(inp) == 17:
        year = int(inp[:4])
        month = int(inp[4:6])
        day = int(inp[6:8])
        hour = int(inp[8:10])
        minute = int(inp[10:12])
    elif len(inp) == 19:
        year = int(inp[:4])
        month = int(inp[5:7])
        day = int(inp[8:10])
        hour = int(inp[11:13])
        minute = int(inp[14:16])
    else:
        raise Exception(f"unknown time column from csv:{inp}")
    return CTime(year, month, day, hour, minute)


class CSV_API(CCommonStockApi):
    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=None):
        self.headers_exist = True  # 第一行是否是标题，如果是数据，设置为False
        self.columns = [
            DATA_FIELD.FIELD_TIME,
            DATA_FIELD.FIELD_OPEN,
            DATA_FIELD.FIELD_HIGH,
            DATA_FIELD.FIELD_LOW,
            DATA_FIELD.FIELD_CLOSE,
            DATA_FIELD.FIELD_VOLUME,
            # DATA_FIELD.FIELD_TURNOVER,
            # DATA_FIELD.FIELD_TURNRATE,
        ]  # 每一列字段
        self.time_column_idx = self.columns.index(DATA_FIELD.FIELD_TIME)
        super(CSV_API, self).__init__(code, k_type, begin_date, end_date, autype)

    def get_kl_data(self):
        # Resolve from CHAN_DATA_DIR and tolerate a missing file: in step mode the
        # klines are fed via trigger_load, so an empty source iterator is correct
        # (and lets one process construct multiple CChan instances safely).
        base = os.environ.get("CHAN_DATA_DIR", os.getcwd())
        file_path = os.path.join(base, f"{self.code}.csv")
        if not os.path.exists(file_path):
            return

        for line_number, line in enumerate(open(file_path, 'r')):
            if self.headers_exist and line_number == 0:
                continue
            data = line.strip("\n").split(",")

            if len(data) != len(self.columns):
                raise CChanException(f"file format error: {file_path}", ErrCode.SRC_DATA_FORMAT_ERROR)
            if self.begin_date is not None and data[self.time_column_idx] < self.begin_date:
                continue
            if self.end_date is not None and data[self.time_column_idx] > self.end_date:
                continue
            yield CKLine_Unit(create_item_dict(data, self.columns), autofix=True)

    def get_kl_data_pd(self):
        file_path = f"{self.code}.csv"
        if not os.path.exists(file_path):
            raise CChanException(f"file not exist: {file_path}", ErrCode.SRC_DATA_NOT_FOUND)

        # 指定 dtype 来确保数据类型一致
        dtype_dict = {0: str}  # 假设第 0 列是时间，并且我们将其读取为字符串
        df = pd.read_csv(file_path, dtype=dtype_dict)

        # 检查列数
        if df.shape[1] != len(self.columns):
            raise CChanException(f"file format error: {file_path}", ErrCode.SRC_DATA_FORMAT_ERROR)

        # 应用日期过滤
        if self.begin_date is not None:
            df = df[df.iloc[:, self.time_column_idx] >= str(self.begin_date)]
        if self.end_date is not None:
            df = df[df.iloc[:, self.time_column_idx] <= str(self.end_date)]

        # 生成 CKLine_Unit 对象
        for index, row in df.iterrows():
            yield CKLine_Unit(create_item_dict(row.tolist(), self.columns), autofix=True)

    def SetBasciInfo(self):
        pass

    @classmethod
    def do_init(cls):
        pass

    @classmethod
    def do_close(cls):
        pass
