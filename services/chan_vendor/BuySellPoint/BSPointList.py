from typing import Dict, Generic, List, Optional, TypeVar, Union, overload

from Common.CEnum import BI_DIR, FX_TYPE
from Bi.Bi import CBi
from Bi.BiList import CBiList
from Common.CEnum import BSP_TYPE
from Common.func_util import has_overlap
from Seg.Seg import CSeg
from Seg.SegListComm import CSegListComm
from ZS.ZS import CZS


from .BS_Point import CBS_Point
from .BSPointConfig import CBSPointConfig, CPointConfig

LINE_TYPE = TypeVar('LINE_TYPE', CBi, CSeg[CBi])
LINE_LIST_TYPE = TypeVar('LINE_LIST_TYPE', CBiList, CSegListComm[CBi])


def find_matching_zs(bsp, segzslist):
    # 遍历 segzslist，找出 begin 相同的 zs
    for zs in segzslist:
        if zs.begin == bsp.zs.begin:
            return zs
    return None  # 如果没有找到匹配的 zs，返回 None
def find_czs_with_target_idx(zs_list, target_idx):
    # 遍历每个 czs
    for czs in zs_list:
        # 检查 czs 中的 bi_out 的 idx 是否与 target_idx 一致
        if czs.bi_out is None:
            return None

        if czs.bi_out.idx == target_idx:
            return czs
    # 如果找不到匹配的，返回 None
    return None

class CBSPointList(Generic[LINE_TYPE, LINE_LIST_TYPE]):
    def __init__(self, bs_point_config: CBSPointConfig,csvdata=None):
        self.lst: List[CBS_Point[LINE_TYPE]] = []
        self.bsp_dict: Dict[int, CBS_Point[LINE_TYPE]] = {}
        self.bsp1_lst: List[CBS_Point[LINE_TYPE]] = []
        self.bsp1o_lst: List[CBS_Point[LINE_TYPE]] = [] # 真正开单的部分

        self.bsp1_seg_lst: List[CBS_Point[LINE_TYPE]] = [] # 线段1类点



        self.bsp2_seg_lst: List[CBS_Point[LINE_TYPE]] = [] # 线段2类点，即bsp2_seg_lst
        self.bsp1_seg_left_makesure_lst: List[CBS_Point[LINE_TYPE]] = [] # 左侧一类点的 1 笔之后的2s类点，其实就是左侧一类点等到右侧
        self.bsp2_seg_left_sure: List[CBS_Point[LINE_TYPE]] = []  # 右侧一类点的 1 笔之后的2类点


        self.bsp2_lst: List[CBS_Point[LINE_TYPE]] = []
        self.bsp3_lst: List[CBS_Point[LINE_TYPE]] = []
        self.bsp4_lst: List[CBS_Point[LINE_TYPE]] = []  # bsp4_lst

        self.config = bs_point_config
        self.last_sure_pos = -1

        if csvdata is not None:
            self.csvdata=csvdata

    def __iter__(self):
        yield from self.lst

    def __len__(self):
        return len(self.lst)

    @overload
    def __getitem__(self, index: int) -> CBS_Point: ...

    @overload
    def __getitem__(self, index: slice) -> List[CBS_Point]: ...

    def __getitem__(self, index: Union[slice, int]) -> Union[List[CBS_Point], CBS_Point]:
        return self.lst[index]

    def cal(self, bi_list: LINE_LIST_TYPE, seg_list: CSegListComm[LINE_TYPE],lst,detail_bi_list,segzs_list):
        '''
        bi_list 和 seg_list 分别代表一个次级别和本级别

        比如如果就是笔和线段，那么一个线段里面就去检查笔中枢和买卖点

        如果是线段 和 线段构成的趋势（多个线段中枢），那么就是在线段趋势里面去看线段的买卖点

        比如k线是1m，那么笔中枢 就是1m走势。线段中枢就是5m走势。线段构成的趋势再去找中枢就是30m级别的

        '''

        # self.lst = [bsp for bsp in self.lst ]
        # self.bsp_dict = {bsp.bi.get_end_klu().idx: bsp for bsp in self.lst}


        self.bsp1o_lst = [bsp for bsp in self.bsp1o_lst]
        self.bsp_dict = {bsp.bi.get_end_klu().idx: bsp for bsp in self.bsp1o_lst}


        self.cal_seg_bs1point(seg_list, bi_list,current_kline=lst[-1],segzs_list=segzs_list)
        self.cal_seg_bs2point(seg_list, bi_list,detail_bi_list,segzs_list)
        self.cal_seg__light_bs2point(seg_list, bi_list,detail_bi_list,segzs_list)
        self.cal_seg__light_bs2point_forleft_point1(seg_list, bi_list,detail_bi_list,segzs_list)
        self.cal_seg_bs3point(seg_list, bi_list)
        self.cal_seg_bs3point_1leftsure(seg_list, bi_list)
        # self.cal_seg_bs3point_old(seg_list, bi_list)

        # 补仓点
        self.cal_seg_bs4point(seg_list, bi_list,detail_bi_list)

        self.update_last_pos(seg_list)
    def bi_cal(self, bi_list: LINE_LIST_TYPE, seg_list: CSegListComm[LINE_TYPE],symbol,SEGZS=None):
        '''
        bi_list 和 seg_list 分别代表一个次级别和本级别

        比如如果就是笔和线段，那么一个线段里面就去检查笔中枢和买卖点

        如果是线段 和 线段构成的趋势（多个线段中枢），那么就是在线段趋势里面去看线段的买卖点

        比如k线是1m，那么笔中枢 就是1m走势。线段中枢就是5m走势。线段构成的趋势再去找中枢就是30m级别的

        '''
        self.lst = [bsp for bsp in self.lst if bsp.klu.idx <= self.last_sure_pos]
        self.bsp_dict = {bsp.bi.get_end_klu().idx: bsp for bsp in self.lst}
        self.bsp1_lst = [bsp for bsp in self.bsp1_lst if bsp.klu.idx <= self.last_sure_pos]


        self.bi_cal_seg_bs1point(seg_list, bi_list)
        self.cal_seg_bs2point(seg_list, bi_list,None,SEGZS)
        # self.cal_seg_bs3point(seg_list, bi_list)

        self.update_last_pos(seg_list)



    def update_last_pos(self, seg_list: CSegListComm):
        self.last_sure_pos = -1
        for seg in seg_list[::-1]:
            # print(seg.is_sure)
            # print(self.last_sure_pos)
            if seg.is_sure:
                self.last_sure_pos = seg.end_bi.get_begin_klu().idx

                return

    def seg_need_cal(self, seg: CSeg):
        return seg.end_bi.get_end_klu().idx > self.last_sure_pos

    def add_bs(
        self,
        bs_type: BSP_TYPE,
        bi: LINE_TYPE,
        relate_bsp1: Optional[CBS_Point],
        is_target_bsp: bool = True,
        feature_dict=None,
            reverse=False,
            is_segbsp=False,
            left_sure=False,
            zs=None

    ):
        if not reverse:
            is_buy = bi.is_down()
        else:
            is_buy = not bi.is_down()
        if relate_bsp1 is not None:
            is_buy=relate_bsp1.is_buy
        if exist_bsp := self.bsp_dict.get(bi.get_end_klu().idx):

            if exist_bsp.is_buy != is_buy:
                '''
                应该按照优先级来，比如如果这是一个前面的三类店 和最近的一类点，那么其实与三类点的一类点可能间隔了很久，所以要按照优先级高的来
                '''
                print('重复点位',exist_bsp.is_buy, is_buy)
                return

            assert exist_bsp.is_buy == is_buy
            exist_bsp.add_another_bsp_prop(bs_type, relate_bsp1)
            return
        if bs_type not in self.config.GetBSConfig(is_buy).target_types:
            is_target_bsp = False

        if is_target_bsp or bs_type in [BSP_TYPE.T1, BSP_TYPE.T1P,BSP_TYPE.T1o,BSP_TYPE.T1D]:
            bsp = CBS_Point[LINE_TYPE](
                bi=bi,
                is_buy=is_buy,
                bs_type=bs_type,
                relate_bsp1=relate_bsp1,
                feature_dict=feature_dict,
                is_segbsp=is_segbsp

            )
            if zs is not None:
                bsp.zs = zs
        else:
            return
        if is_target_bsp:
            self.lst.append(bsp)
            self.bsp_dict[bi.get_end_klu().idx] = bsp
        if is_target_bsp and  bs_type in [BSP_TYPE.T1, BSP_TYPE.T1P,BSP_TYPE.T1o,BSP_TYPE.T1D] and not is_segbsp:# 这里的is seg 是第二种跌打的买卖点
            self.bsp1_lst.append(bsp)
        if is_target_bsp and bs_type in [BSP_TYPE.T1, BSP_TYPE.T1P] and is_segbsp:
            self.bsp1_seg_lst.append(bsp) # 这里面只能是经过简单线段背驰带来的  而不是经过确认的
        if bs_type in [BSP_TYPE.T1o,BSP_TYPE.T1D]:
            self.bsp1o_lst.append(bsp) # 这里是确认的

        if bs_type in [BSP_TYPE.T2] and not is_segbsp:
            self.bsp2_lst.append(bsp)

        if bs_type in [BSP_TYPE.T2] and is_segbsp:
            self.bsp2_seg_lst.append(bsp)

            if left_sure:
                self.bsp2_seg_left_sure.append(bsp) # 经确认后的左侧一类点，其确认点的2类点

        if bs_type in [BSP_TYPE.T2S] and is_segbsp:
            self.bsp1_seg_left_makesure_lst.append(bsp)
            self.bsp2_seg_lst.append(bsp)



        if bs_type in [BSP_TYPE.T3A,BSP_TYPE.T3B]:
            self.bsp3_lst.append(bsp)
        if bs_type in [BSP_TYPE.T4]:
            self.bsp4_lst.append(bsp)

    def add_bs3(
        self,
        bs_type: BSP_TYPE,
        bi: LINE_TYPE,
        relate_bsp1: Optional[CBS_Point],
        is_target_bsp: bool = True,
        feature_dict=None,
            reverse=False,

    ):
        if not reverse:
            is_buy = bi.is_down()
        else:
            is_buy = not bi.is_down()

        bsp = CBS_Point[LINE_TYPE](
            bi=bi,
            is_buy=is_buy,
            bs_type=bs_type,
            relate_bsp1=relate_bsp1,
            feature_dict=feature_dict,

        )
        bsp.is_segbsp = True

        self.lst.append(bsp)
        self.bsp_dict[bi.get_end_klu().idx] = bsp

        self.bsp3_lst.append(bsp)


    def cal_seg_bs1point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE,current_kline,segzs_list):
        if len(seg_list) == 0:
            for seg in seg_list:
                if not self.seg_need_cal(seg):
                    continue
                self.cal_single_bs1point(seg, bi_list,current_kline,segzs_list)

        # only use the last seg
        else:
            seg=seg_list[-1]
            if not self.seg_need_cal(seg):
                return

            self.cal_single_bs1point(seg, bi_list,current_kline,segzs_list)
    def bi_cal_seg_bs1point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):

        if len(seg_list) == 0:
            for seg in seg_list:
                if not self.seg_need_cal(seg):
                    continue
                self.bi_cal_single_bs1point(seg, bi_list)
        # only use the last seg
        else:
            seg=seg_list[-1]
            if not self.seg_need_cal(seg):
                return
            self.bi_cal_single_bs1point(seg, bi_list)





    def cal_single_bs1point(self, seg: CSeg[LINE_TYPE], bi_list: LINE_LIST_TYPE,current_kline,segzs_list,allow_only_multibi_zs=False):
        '''
        对于线段中枢来说，这里的bi_list 其实是一个线段的编号
        判断线段的更高级别（30min级别）内部是否包含中枢，也就是线段级别上是否存在中枢

        以及是否考虑一笔的中枢

        '''
        # if len(seg.zs_lst)>0:
            # print(seg.end_bi.idx,seg.zs_lst[-1].bi_out.idx,seg.zs_lst[-1].get_bi_in().idx,seg.zs_lst[-1].bi_lst[-1].idx)
        if allow_only_multibi_zs:
            BSP_CONF = self.config.GetBSConfig(seg.is_down())
            zs_cnt = seg.get_multi_bi_zs_cnt() if BSP_CONF.bsp1_only_multibi_zs else len(seg.zs_lst)
            is_target_bsp = (BSP_CONF.min_zs_cnt <= 0 or zs_cnt >= BSP_CONF.min_zs_cnt)
            if len(seg.zs_lst) > 1 and \
                    not seg.zs_lst[-1].is_one_bi_zs() and \
                    ((seg.zs_lst[-1].bi_out and seg.zs_lst[-1].bi_out.idx >= (seg.end_bi.idx-1)) or seg.zs_lst[-1].bi_lst[
                        -1].idx >= (seg.end_bi.idx-1)) and \
                    seg.end_bi.idx - seg.zs_lst[-1].get_bi_in().idx >= 2:

                    if  seg.dir == BI_DIR.UP and seg.zs_lst[-1].mid > seg.zs_lst[-2].mid:
                        self.treat_seg_bsp1(seg, BSP_CONF, is_target_bsp,segzs_list)

                    elif     (seg.dir == BI_DIR.DOWN and seg.zs_lst[-1].mid < seg.zs_lst[-2].mid):
                        self.treat_seg_bsp1(seg, BSP_CONF, is_target_bsp,segzs_list)

                    else:
                        self.treat_Reverse_Segpz_bsp1(seg, BSP_CONF, bi_list, is_target_bsp,segzs_list)
            elif len(seg.zs_lst) > 0 and \
               not seg.zs_lst[-1].is_one_bi_zs() and \
               ((seg.zs_lst[-1].bi_out and seg.zs_lst[-1].bi_out.idx >= seg.end_bi.idx) or seg.zs_lst[-1].bi_lst[-1].idx >= seg.end_bi.idx) \
               and seg.end_bi.idx - seg.zs_lst[-1].get_bi_in().idx >=2 :
                #盘整背驰点之间，不能超过3个线段，要不然都是新的一段了；
                self.treat_Segpz_bsp1(seg, BSP_CONF,bi_list, is_target_bsp,segzs_list)

        else:

            last_bsp1=self.bsp1_seg_lst[-1] if len(self.bsp1_seg_lst)>0 else None
            if last_bsp1 is not None:
                if last_bsp1.bi.idx == seg.end_bi.idx: # 这个笔上已经有了一个买卖点，就不能有第二个了
                    return

            BSP_CONF = self.config.GetBSConfig(False)
            zs_cnt = seg.get_multi_bi_zs_cnt() if BSP_CONF.bsp1_only_multibi_zs else len(seg.zs_lst)
            is_target_bsp = (BSP_CONF.min_zs_cnt <= 0 or zs_cnt >= BSP_CONF.min_zs_cnt)
            if len(seg.zs_lst) > 1 and \
                    ((seg.zs_lst[-1].bi_out and seg.zs_lst[-1].bi_out.idx >= (seg.end_bi.idx - 1)) or
                     seg.zs_lst[-1].bi_lst[
                         -1].idx >= (seg.end_bi.idx - 1)) and \
                    seg.end_bi.idx - seg.zs_lst[-1].get_bi_in().idx >= 2:

                if seg.dir == BI_DIR.UP and seg.zs_lst[-1].high > seg.zs_lst[-2].high:
                    self.treat_seg_bsp1(seg, BSP_CONF, is_target_bsp,current_kline,segzs_list)

                elif (seg.dir == BI_DIR.DOWN and seg.zs_lst[-1].low < seg.zs_lst[-2].low):
                    self.treat_seg_bsp1(seg, BSP_CONF, is_target_bsp,current_kline,segzs_list)

                else:
                    self.treat_Reverse_Segpz_bsp1(seg, BSP_CONF, bi_list, is_target_bsp,current_kline,segzs_list)
            elif len(seg.zs_lst) > 0 and \
                    ((seg.zs_lst[-1].bi_out and seg.zs_lst[-1].bi_out.idx >= seg.end_bi.idx) or seg.zs_lst[-1].bi_lst[
                        -1].idx >= seg.end_bi.idx) \
                    and seg.end_bi.idx - seg.zs_lst[-1].get_bi_in().idx >= 2:
                # 盘整背驰点之间，不能超过3个线段，要不然都是新的一段了；
                self.treat_Segpz_bsp1(seg, BSP_CONF, bi_list, is_target_bsp,current_kline,segzs_list)


        # else:
        #     self.treat_pz_bsp1(seg, BSP_CONF, bi_list, is_target_bsp)
    def bi_cal_single_bs1point(self, seg: CSeg[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        BSP_CONF = self.config.GetBSConfig(seg.is_down())
        zs_cnt = seg.get_multi_bi_zs_cnt() if BSP_CONF.bsp1_only_multibi_zs else len(seg.zs_lst)
        is_target_bsp = (BSP_CONF.min_zs_cnt <= 0 or zs_cnt >= BSP_CONF.min_zs_cnt)
        if len(seg.zs_lst) > 1 and \
           not seg.zs_lst[-1].is_one_bi_zs() and \
           ((seg.zs_lst[-1].bi_out and seg.zs_lst[-1].bi_out.idx >= seg.end_bi.idx) or seg.zs_lst[-1].bi_lst[-1].idx >= seg.end_bi.idx) \
           and seg.end_bi.idx - seg.zs_lst[-1].get_bi_in().idx > 2:
            self.treat_bsp1(seg, BSP_CONF, is_target_bsp)

        elif len(seg.zs_lst) > 0 and \
                not seg.zs_lst[-1].is_one_bi_zs() and \
                ((seg.zs_lst[-1].bi_out and seg.zs_lst[-1].bi_out.idx >= seg.end_bi.idx) or seg.zs_lst[-1].bi_lst[
                    -1].idx >= seg.end_bi.idx) \
                and seg.end_bi.idx - seg.zs_lst[-1].get_bi_in().idx > 2:
            self.treat_bsp1(seg, BSP_CONF,  is_target_bsp)

        # 笔也要有一个中枢啊
        else:
            self.treat_pz_bsp1(seg, BSP_CONF, bi_list, is_target_bsp)
    def treat_bsp1(self, seg: CSeg[LINE_TYPE], BSP_CONF: CPointConfig, is_target_bsp: bool):
        '''
        首先判断最后离开中枢的一笔（不属于中枢）是否break，按照线段级别，应该是判断出去这一笔是否高于中枢的高点，或者中枢区间内的最高点。
        然后比较进去中枢和离开中枢的两个线段（都不属于中枢）的本级别macd的力度

        笔中枢不判断最低点
        '''
        last_zs = seg.zs_lst[-1]
        break_peak, _ = last_zs.out_bi_is_peak(seg.end_bi.idx)
        break_peak_shangxiayuan,_= last_zs.out_bi_is_peak_vszs(seg.end_bi.idx)

        break_peak=break_peak or break_peak_shangxiayuan

        if BSP_CONF.bs1_peak and not break_peak:
            is_target_bsp = False
        is_diver, divergence_rate = last_zs.is_divergence(BSP_CONF, out_bi=seg.end_bi)
        if not is_diver:
            is_target_bsp = False
        feature_dict = {'divergence_rate': divergence_rate}
        # if is_target_bsp:
        #     print('bi 级别趋势背驰, divergence_rate: %s' % (divergence_rate))
        self.add_bs(bs_type=BSP_TYPE.T1, bi=seg.end_bi, relate_bsp1=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict)

    def treat_seg_bsp1(self, seg: CSeg[LINE_TYPE], BSP_CONF: CPointConfig, is_target_bsp: bool,current_kline,segzs_list):
        '''
        首先判断最后离开中枢的一笔（不属于中枢）是否break，按照线段级别，应该是判断出去这一笔是否高于中枢的高点，或者中枢区间内的最高点。
        然后比较进去中枢和离开中枢的两个线段（都不属于中枢）的本级别macd的力度

        怎么判断走完呢？ 中枢的outbi这一个线段是有三个笔，而且最后的最新的一笔是反向的，比如向上的线段的时候就是向下的，
        因为至少有比如有3个向下的，才能确定一个新的序列定底分
        '''
        last_zs = seg.zs_lst[-1]

        # 当前这个k线首先是脱离了中枢的，才有谈的基础
        onebi=last_zs.is_one_bi_zs()
        if  not (current_kline.low>last_zs.high or current_kline.high<last_zs.low):
            return

        break_peak, _ = last_zs.out_bi_is_peak(seg.end_bi.idx)
        if BSP_CONF.bs1_peak and not break_peak:
            is_target_bsp = False
        #is_diver, divergence_rate = last_zs.is_divergence(BSP_CONF, out_bi=seg.end_bi)
        is_Seg_diver, divergence_rate_Seg = last_zs.is_Seg_divergence(BSP_CONF, out_bi=seg.end_bi,use_cross=False)

        if not is_Seg_diver:
            is_target_bsp = False
        feature_dict = {'divergence_rate': divergence_rate_Seg}
        self.add_bs(bs_type=BSP_TYPE.T1, bi=seg.end_bi, relate_bsp1=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict,is_segbsp=True,zs=segzs_list[-1])

    # @staticmethod
    def treat_seg_bsp1_solo( self,seg: CSeg[LINE_TYPE], BSP_CONF: CPointConfig,last_zs, is_target_bsp: bool):
        '''
        首先判断最后离开中枢的一笔（不属于中枢）是否break，按照线段级别，应该是判断出去这一笔是否高于中枢的高点，或者中枢区间内的最高点。
        然后比较进去中枢和离开中枢的两个线段（都不属于中枢）的本级别macd的力度

        怎么判断走完呢？ 中枢的outbi这一个线段是有三个笔，而且最后的最新的一笔是反向的，比如向上的线段的时候就是向下的，
        因为至少有比如有3个向下的，才能确定一个新的序列定底分
        '''
        if last_zs is  None:
            last_zs = seg.zs_lst[-1]
        break_peak, _ = last_zs.out_bi_is_peak(seg.end_bi.idx)
        if  not break_peak:
            is_target_bsp = False
        #is_diver, divergence_rate = last_zs.is_divergence(BSP_CONF, out_bi=seg.end_bi)

        # if seg.end_bi.idx-last_zs.bi_out.idx>=2:
        #     in_bi=seg.bi_list[-3] # 出笔是倒数第一笔，

        is_Seg_diver, divergence_rate_Seg = last_zs.is_Seg_divergence(BSP_CONF, out_bi=seg.end_bi)

        if not is_Seg_diver:
            is_target_bsp = False
        feature_dict = {'divergence_rate': divergence_rate_Seg}

        if is_target_bsp:
            self.add_bs(bs_type=BSP_TYPE.T1o, bi=last_zs.bi_out, relate_bsp1=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict,zs=last_zs)
        return is_target_bsp,feature_dict

    def treat_seg_bsp1_bisolo( self,seg: CSeg[LINE_TYPE],current_bi, BSP_CONF: CPointConfig,last_zs,relate_bsp1,seglast_zs,is_target_bsp: bool):
        '''
        首先判断最后离开中枢的一笔（不属于中枢）是否break，按照线段级别，应该是判断出去这一笔是否高于中枢的高点，或者中枢区间内的最高点。
        然后比较进去中枢和离开中枢的两个线段（都不属于中枢）的本级别macd的力度

        怎么判断走完呢？ 中枢的outbi这一个线段是有三个笔，而且最后的最新的一笔是反向的，比如向上的线段的时候就是向下的，
        因为至少有比如有3个向下的，才能确定一个新的序列定底分
        relate_bsp1: 就是最新的一类点
        '''
        if last_zs is None:
            last_valid_zs = None

            for zs in reversed(seg.zs_lst):
                if zs.end_bi.idx < seg.bi_list[-1].idx:
                    last_valid_zs = zs
                    break
            last_zs=last_valid_zs
        # last_valid_zs 现在保存的是满足条件的最后一个 zs，如果没有找到则为 None

        if last_zs is None:
            return False,None
        break_peak, _ = last_zs.out_bi_is_peak(seg.end_bi.idx)
        if  not break_peak:
            is_target_bsp = False
        #is_diver, divergence_rate = last_zs.is_divergence(BSP_CONF, out_bi=seg.end_bi)

        # if seg.end_bi.idx-last_zs.bi_out.idx>=2:
        #     in_bi=seg.bi_list[-3] # 出笔是倒数第一笔，

        is_Seg_diver, divergence_rate_Seg = last_zs.is_divergence(BSP_CONF, out_bi=seg.end_bi)

        if not is_Seg_diver:
            is_target_bsp = False
        feature_dict = {'divergence_rate': divergence_rate_Seg}

        if is_target_bsp and is_Seg_diver:#1D 是1类点的确认 所以这个时候，的bi 一定不是zs的 bi out 而应该是当前的笔
            self.add_bs(bs_type=BSP_TYPE.T1D, bi=seg, relate_bsp1=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict,is_segbsp=True,zs=seglast_zs)
        return is_target_bsp,feature_dict


    def treat_pz_bsp1(self, seg: CSeg[LINE_TYPE], BSP_CONF: CPointConfig, bi_list: LINE_LIST_TYPE, is_target_bsp):
        last_bi = seg.end_bi
        pre_bi = bi_list[last_bi.idx-2]
        if last_bi.seg_idx != pre_bi.seg_idx:
            return
        if last_bi.dir != seg.dir:
            return
        if last_bi.is_down() and last_bi._low() > pre_bi._low():  # 创新低
            return
        if last_bi.is_up() and last_bi._high() < pre_bi._high():  # 创新高
            return

        in_metric = pre_bi.cal_macd_metric(BSP_CONF.macd_algo)
        out_metric = last_bi.cal_macd_metric(BSP_CONF.macd_algo)
        is_diver, divergence_rate = out_metric <= BSP_CONF.divergence_rate*in_metric, out_metric/(in_metric+1e-7)
        if not is_diver:
            is_target_bsp = False
        if isinstance(bi_list, CBiList):
            assert isinstance(last_bi, CBi) and isinstance(pre_bi, CBi)
        feature_dict = {'divergence_rate': divergence_rate}
        # if is_target_bsp:
        #     print('bi 级别盘整背驰，in_metric: %s, out_metric: %s, divergence_rate: %s' % (in_metric, out_metric, divergence_rate))
        self.add_bs(bs_type=BSP_TYPE.T1P, bi=last_bi, relate_bsp1=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict)

    def treat_Segpz_bsp1(self, seg: CSeg[LINE_TYPE], BSP_CONF: CPointConfig, bi_list: LINE_LIST_TYPE, is_target_bsp,current_kline,segzs_list):
        '''
        线段盘整背驰，也有5段线，现在是第5段线的笔，这个笔要高于原来的中枢(out_bi_is_peak)，且出现背驰
        盘整背驰通常考虑同向相邻的两端，我们就以中枢里面的前后两端来作为比较,当然 用 中枢的进入和出也是可以的
        '''



        last_zs = seg.zs_lst[-1]

        # 当前这个k线首先是脱离了中枢的，才有谈的基础
        if  not (current_kline.low>last_zs.high or current_kline.high<last_zs.low):
            return

        break_peak, _ = last_zs.out_bi_is_peak(seg.end_bi.idx)
        if BSP_CONF.bs1_peak and not break_peak:
            is_target_bsp = False
        is_diver, divergence_rate = last_zs.is_SegPZ_divergence(BSP_CONF, out_bi=seg.end_bi,num_bis=len(seg.bi_list),use_cross=False)
        if not is_diver:
            is_target_bsp = False
        feature_dict = {'divergence_rate': divergence_rate}
        self.add_bs(bs_type=BSP_TYPE.T1P, bi=seg.end_bi, relate_bsp1=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict,is_segbsp=True,zs=segzs_list[-1])

    def treat_Reverse_Segpz_bsp1(self, seg: CSeg[LINE_TYPE], BSP_CONF: CPointConfig, bi_list: LINE_LIST_TYPE, is_target_bsp,current_kline,segzs_list):
        '''
        线段盘整背驰，也有5段线，现在是第5段线的笔，这个笔要高于原来的中枢(out_bi_is_peak)，且出现背驰
        '''



        last_zs = seg.zs_lst[-1]
        # 当前这个k线首先是脱离了中枢的，才有谈的基础
        if  not (current_kline.low>last_zs.high or current_kline.high<last_zs.low):
            return

        break_peak, _ = last_zs.out_bi_is_peak(seg.end_bi.idx)
        if BSP_CONF.bs1_peak and not break_peak:
            is_target_bsp = False
        is_diver, divergence_rate = last_zs.is_Reverse_SegPZ_divergence(BSP_CONF, out_bi=seg.end_bi)
        if not is_diver:
            is_target_bsp = False
        feature_dict = {'divergence_rate': divergence_rate}
        self.add_bs(bs_type=BSP_TYPE.T1PR, bi=seg.end_bi, relate_bsp1=None, is_target_bsp=is_target_bsp, feature_dict=feature_dict,reverse=True,zs=segzs_list[-1])


    def cal_seg_bs2point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE,detail_bi_list,segzs_list):
        bsp1_bi_idx_dict = {bsp.bi.idx: bsp for bsp in self.bsp1o_lst}

        if len( bsp1_bi_idx_dict)<1 or len(seg_list) < 1:
            return

        bsp=self.bsp1o_lst[-1]
        newest_bsp1_bi_idx_dict = {bsp.bi.idx: bsp }

        bsp1_bi=bsp.bi.idx+1
        newest_bi=bi_list[-1].idx

        # Create a list to hold every bsp1_bi + even number (2, 4, 6, ...) until it is >= newest_bi
        idx_list = []
        current_bi = bsp1_bi
        while current_bi <= newest_bi:
            idx_list.append(current_bi)
            current_bi += 2

        # 找到zs
        # bsp1_seg_idx=bsp.bi.seg_idx # bsp1 属于哪一个大的seg，中枢是这个seg里面的最后一个
        # zs = seg_list[bsp1_seg_idx].zs_lst[-1]

        if len(self.bsp2_lst)>=1:
            # 获取 bsp1o_lst 和 bsp2_lst 中的最后一个 bsp 对象
            bsp1_last = self.bsp1o_lst[-1]
            bsp2_last = self.bsp2_lst[-1]
            # 获取最后一个点的时间
            bsp1_last_time = bsp1_last.klu.time
            bsp2_last_time = bsp2_last.klu.time

            if bsp1_last_time <= bsp2_last_time:
                # print('已经出现了第二类买点，后续同类型跳过')
                return

        matching_zs = find_matching_zs(bsp, segzs_list)
        if matching_zs is not None:
            # 二类点是否要求 不能近中枢
            if detail_bi_list.last_end.lst[-1].close<matching_zs.high and detail_bi_list.last_end.lst[-1].close>matching_zs.low:
                    # del self.bsp1_seg_lst[-1]
                return
            if matching_zs.end.time>bsp.klu.time:
                    # 说明其实中枢拓展了，所以要把前一个一类点删除，这个不算了
                # print(f'中枢拓展了，所以要把{bsp.klu.time}一类点删除，这个不算了')
                return




        # Extract the high values corresponding to the selected indices
        peak_points = []
        for idx in idx_list:
            if idx < len(bi_list):
                if bsp.is_buy:
                    peak_points.append(bi_list[idx].start_bi.begin_klc.low)
                else:
                    peak_points.append(bi_list[idx].start_bi.begin_klc.high)

        # Compare the latest high point with the maximum high point in the list
        if peak_points:
            latest_high_point = peak_points[-1]
            if bsp.is_buy:
                max_peak_point = min(peak_points)
            else:
                max_peak_point = max(peak_points)

            # Check if the latest high point is NOT the maximum value
            if latest_high_point != max_peak_point:


                self.treat_bsp2_seg(seg_list[-1], bsp1_bi_idx_dict, seg_list, bi_list,bsp.bi)
                return True  # The latest high point is not the highest
            else:
                return False  # The latest high point is the highest
        return False




        # for bsp in self.bsp1o_lst:
        # for seg in seg_list:
        #     config = self.config.GetBSConfig(seg.is_down())
        #     if BSP_TYPE.T2 not in config.target_types and BSP_TYPE.T2S not in config.target_types:
        #         continue
        #     self.treat_bsp2(seg, bsp1_bi_idx_dict, seg_list, bi_list)


    def cal_seg__light_bs2point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE,detail_bi_list,segzs_list):
        '''
        一类点 坑开在左侧或者右侧
        如果是左侧，说明bsp的笔方向与买卖点方向相反 这个时候二类点（类一）是下一个(n+1)向下的笔的起始
        如果是右侧，说明bsp的笔方向与买卖点方向相通 这个时候二类点（类一）是下一个(n+2)向下的笔的起始
        无论哪种与峰值是否创新高其实无关
        '''
        bsp1_bi_idx_dict = {bsp.bi.idx: bsp for bsp in self.bsp1_seg_lst}

        if len( bsp1_bi_idx_dict)<1 or len(seg_list) < 1:
            return

        bsp=self.bsp1_seg_lst[-1]
        newest_bsp1_bi_idx_dict = {bsp.bi.idx: bsp }

        bsp1_bi=bsp.bi.idx    # 由于seg的idx 可能会发生变化，所以最好用bi的idx，再反推出来seg的idx

        if len(bsp.bi.bi_list)>0 :
            if bsp.bi.bi_list[-1].idx in detail_bi_list:
                bsp1_bi_detail = detail_bi_list[bsp.bi.bi_list[-1].idx].seg_idx

                if bsp1_bi_detail !=bsp1_bi:
                    print('bs1_detail_bi_idx不等于bs1_bi_idx')
                    bsp1_bi=bsp1_bi_detail
                    self.bsp1_seg_lst[-1].bi.idx=bsp1_bi_detail

        newest_bi=bi_list[-1].idx

        if len(self.bsp2_seg_lst)>=1:
            # 获取 bsp1o_lst 和 bsp2_lst 中的最后一个 bsp 对象
            bsp1_last = self.bsp1_seg_lst[-1]
            bsp2_last = self.bsp2_seg_lst[-1]
            # 获取最后一个点的时间
            bsp1_last_time = bsp1_last.klu.time
            bsp2_last_time = bsp2_last.klu.time

            if bsp1_last_time <= bsp2_last_time:
                # print('已经出现了第二类买点，后续同类型跳过')
                return

        # 判断左右侧
        if bsp.left_right=='right':
           # 右侧买点
            target_2_class_bi_idx=bsp1_bi+2
            # 确认一下
            # if not (bsp.is_buy == False and bsp.bi.dir==BI_DIR.DOWN or bsp.is_buy == True and bsp.bi.dir==BI_DIR.UP):
            #     return


        else:
            target_2_class_bi_idx=bsp1_bi+1 # 或者加3 这个要测试  加1 是要把左侧点在右侧确认，右侧需要有两个笔，一方面是确认趋势，另一方面是2个笔还是顶，更合适

        matching_zs = find_matching_zs(bsp, segzs_list)
        if matching_zs is not None: # 找不到说明更新了

            try:
                aa=detail_bi_list.last_end.lst[-1].close
            except:
                print('找不到匹配的中枢')



            if detail_bi_list.last_end.lst[-1].close<matching_zs.high and detail_bi_list.last_end.lst[-1].close>matching_zs.low:
                    # del self.bsp1_seg_lst[-1]
                return
            if matching_zs.end.time>bsp.klu.time:
                    # 说明其实中枢拓展了，所以要把前一个一类点删除，这个不算了
                # print(f'中枢拓展了，所以要把{bsp.klu.time}一类点删除，这个不算了')
                return

        if newest_bi < target_2_class_bi_idx:
            # 没达到目标线段
            return
        else: # 到达目标线段 还要看看有没有 两个笔 或者有一个在第一个笔之后的分型

            # if bi_list[target_2_class_bi_idx].bi_list[0].is_sure:
            #     print('新线段的第一笔确认')
            #
            #
            # last_lst=lst[-1]
            # if bsp.is_buy and last_lst.fx==FX_TYPE.TOP:
            #     print('buy 顶分确认')
            # elif not bsp.is_buy and last_lst.fx==FX_TYPE.BOTTOM:
            #     print('sell 底分确认')

            if len(bi_list[target_2_class_bi_idx].bi_list)<2:
                return

        # last_lsttest = lst[-100:]


        # if target_2_class_bi_idx==bsp1_bi+1:
        #     # 重新计算背驰
        #     left_1point_seg=seg_list[bsp.bi.seg_idx]
        #     is_target_bsp,feature_dict=self.treat_seg_bsp1_solo(left_1point_seg, self.config.GetBSConfig(bsp.is_buy), None, True)
        #     if is_target_bsp:
        #         self.treat_bsp2_seg_light(seg_list[-1], newest_bsp1_bi_idx_dict, seg_list, bi_list,left=True)
        # if target_2_class_bi_idx==bsp1_bi+2:
        #     self.treat_bsp2_seg_light(seg_list[-1], bsp1_bi_idx_dict, seg_list, bi_list,
        #                               left=(target_2_class_bi_idx == bsp1_bi + 1))






        break_bi=bi_list[bsp1_bi]
        bsp2_bi=bi_list[bsp1_bi+1]
        self.treat_bsp2_seg_light(break_bi,bsp2_bi, bsp, seg_list, bi_list,left=(target_2_class_bi_idx==bsp1_bi+1),zs=matching_zs)

    def cal_seg__light_bs2point_forleft_point1(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE,detail_bi_list,segzs_list):
        '''
        一类点 坑开在左侧或者右侧
        如果是左侧，说明bsp的笔方向与买卖点方向相反 这个时候二类点（类一）是下一个(n+1)向下的笔的起始
        如果是右侧，说明bsp的笔方向与买卖点方向相通 这个时候二类点（类一）是下一个(n+2)向下的笔的起始
        无论哪种与峰值是否创新高其实无关
        '''
        bsp1_bi_idx_dict = {bsp.bi.idx: bsp for bsp in self.bsp1_seg_left_makesure_lst}

        if len( bsp1_bi_idx_dict)<1 or len(seg_list) < 1:
            return


        bsp=self.bsp1_seg_left_makesure_lst[-1]

        if   bsp.zs is not None:
            matching_zs = find_matching_zs(bsp, segzs_list)

            if matching_zs is not None :
                if detail_bi_list.last_end.lst[-1].close<matching_zs.high and detail_bi_list.last_end.lst[-1].close>matching_zs.low:
                        # del self.bsp1_seg_lst[-1]
                    return
                if matching_zs.end.time>bsp.klu.time:
                        # 说明其实中枢拓展了，所以要把前一个一类点删除，这个不算了
                    # print(f'中枢拓展了，所以要把{bsp.klu.time}一类点删除，这个不算了')
                    return


        bsp1_bi=bsp.bi.idx
        newest_bi=bi_list[-1].idx

        if len(self.bsp2_seg_lst)>=1:
            # 获取 bsp1o_lst 和 bsp2_lst 中的最后一个 bsp 对象
            bsp1_last = self.bsp1_seg_left_makesure_lst[-1]
            bsp2_last = self.bsp2_seg_lst[-1]
            # 获取最后一个点的时间
            bsp1_last_time = bsp1_last.klu.time
            bsp2_last_time = bsp2_last.klu.time

            if bsp1_last_time < bsp2_last_time:
                # print('已经出现了第二类买点，后续同类型跳过')
                return

        # 判断左右侧
        if bsp.is_buy and bsp.bi.dir == BI_DIR.UP:
           # 右侧买点
            target_2_class_bi_idx=bsp1_bi+2
        elif not bsp.is_buy and bsp.bi.dir == BI_DIR.DOWN: # 右侧卖点
            target_2_class_bi_idx = bsp1_bi + 2
        else:
            target_2_class_bi_idx=bsp1_bi+1 # 或者加3 这个要测试  加1 是要把左侧点在右侧确认，右侧需要有两个笔，一方面是确认趋势，另一方面是2个笔还是顶，更合适


        if newest_bi < target_2_class_bi_idx:
            # 没达到目标线段
            return
        elif newest_bi > target_2_class_bi_idx:
            return # 过了
        else: # 到达目标线段 还要看看有没有 两个笔 或者有一个在第一个笔之后的分型
            self.add_bs(bs_type=BSP_TYPE.T2, bi=seg_list[-1], relate_bsp1=bsp, reverse=True,
                        is_segbsp=True,left_sure=True)  # 这个时候卖点出现在下降笔，所以要reverse
            # self.treat_bsp2_seg_light_for_class_left1(seg_list[-1], bsp, seg_list, bi_list,left=False)






    def treat_bsp2_seg(self, seg: CSeg, bsp1_bi_idx_dict, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE,bsp1_bi):
        '''
        二卖的逻辑是一买后面第一个不创新高的顶分，（不创新高，是指从bsp1_bi 这一笔的末尾笔，也就是一买之前的那个顶分开始往后算）
        如果出现了一个顶分，且这个顶分是【是指从bsp1_bi，】非最高点，那就是二买了

        但这里有力度区分，在中枢上，中，还是下，

        也就是上一个一类买卖点的idx 的高点，然后当我我后面形成了线段的顶分，那就是二买了
        也就是idx+1+3+5这些线段的起点，如果能看到什么时候，idx+n的起点不是最高的时候，那颗时刻就是了

        bsp1_bi： 出现一类的买点的线段，可以能是向下的，如果是左侧的话，也可能是向上的，如果是右侧

        '''

        if not self.seg_need_cal(seg):
            return
        if len(seg_list) > 1:
            BSP_CONF = self.config.GetBSConfig(seg.is_down())
            if bsp1_bi is not None:
                bsp1_bi_idx = bsp1_bi.idx
                real_bsp1 = bsp1_bi_idx_dict.get(bsp1_bi.idx)
                if real_bsp1 is None:
                    return
                if real_bsp1.is_buy and real_bsp1.bi.dir == BI_DIR.DOWN:
                    # 说明出现在左侧
                    break_bi = bi_list[bsp1_bi.idx+1]
                elif real_bsp1.is_buy and real_bsp1.bi.dir == BI_DIR.UP:
                    # 说明出现在右侧
                    break_bi = bi_list[bsp1_bi.idx ]
                elif not real_bsp1.is_buy and real_bsp1.bi.dir == BI_DIR.DOWN:
                    # 说明出现在右侧
                    break_bi = bi_list[bsp1_bi.idx ]
                elif not real_bsp1.is_buy and real_bsp1.bi.dir == BI_DIR.UP:
                    # 说明出现在左侧
                    break_bi = bi_list[bsp1_bi.idx+1]
                else:
                    print('二类点没能确认 break_bi')
            # 对于bsp2的笔而言，如果是买点，最后一个笔是up则看前一笔，否则就是当前笔
            # 对于bsp2的笔而言，如果是卖点，最后一个笔是down则看前一笔，否则就是当前笔
                if real_bsp1.is_buy and bi_list[-1].dir == BI_DIR.UP:
                    bsp2_bi = bi_list[-2]
                elif real_bsp1.is_buy and bi_list[-1].dir == BI_DIR.DOWN:
                    bsp2_bi = bi_list[-1]
                elif not real_bsp1.is_buy and bi_list[-1].dir == BI_DIR.UP:
                    bsp2_bi = bi_list[-1]
                elif not real_bsp1.is_buy and bi_list[-1].dir == BI_DIR.DOWN:
                    bsp2_bi = bi_list[-2]
                else:
                    print('二类点没能确认 bsp2_bi')




            else:
                bsp1_bi = seg.start_bi # 否则取新的部分的第一个线段
                bsp1_bi_idx = bsp1_bi.idx -1 ## 因为我们的一类点都是延迟确认的，而确认一类买点的时候，其实买点那一笔没有走完，所以这个笔往往比，实际的早1笔。而实际二类买点会在一买后面最少2 笔，
                # 找出来break 和当前bsp的笔
                break_bi = bi_list[bsp1_bi.idx ]
                bsp2_bi = bi_list[bsp1_bi.idx + 1]

                real_bsp1 = bsp1_bi_idx_dict.get(bsp1_bi.idx-1)
            if bsp1_bi.idx + 2  >= len(bi_list):  # 因为我们的一类点都是延迟确认的，因为一买出现的笔滞后了，二买是一买出现后再回调，所以间隔为2
                return


        else:
            BSP_CONF = self.config.GetBSConfig(seg.is_up())
            bsp1_bi, real_bsp1 = None, None
            bsp1_bi_idx = -1
            if len(bi_list) == 1:
                return
            bsp2_bi = bi_list[1]
            break_bi = bi_list[0]
        if BSP_CONF.bsp2_follow_1 and bsp1_bi_idx not in [bsp.bi.idx for bsp in self.bsp_dict.values()]:  # check bsp2_follow_1
            return
        retrace_rate = bsp2_bi.amp()/break_bi.amp()
        bsp2_flag = retrace_rate <= BSP_CONF.max_bs2_rate
        if bsp2_flag:
            state = 'buy' if real_bsp1.is_buy else 'sell'
            print(f'确认一个一二类{state}点')
            self.add_bs(bs_type=BSP_TYPE.T2, bi=bsp2_bi, relate_bsp1=real_bsp1)  # type: ignore
        elif BSP_CONF.bsp2s_follow_2:
            return
        if BSP_TYPE.T2S not in self.config.GetBSConfig(seg.is_down()).target_types:
            return
        self.treat_bsp2s(seg_list, bi_list, bsp2_bi, break_bi, real_bsp1, BSP_CONF)  # type: ignore


    def treat_bsp2_seg_light(self, break_bi,bsp2_bi, real_bsp1, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE,left,zs):
        '''
        二卖的逻辑是一买后面第一个不创新高的顶分，（不创新高，是指从bsp1_bi 这一笔的末尾笔，也就是一买之前的那个顶分开始往后算）
        如果出现了一个顶分，且这个顶分是【是指从bsp1_bi，】非最高点，那就是二买了

        但这里有力度区分，在中枢上，中，还是下，

        也就是上一个一类买卖点的idx 的高点，然后当我我后面形成了线段的顶分，那就是二买了
        也就是idx+1+3+5这些线段的起点，如果能看到什么时候，idx+n的起点不是最高的时候，那颗时刻就是了

        如果原来的一类点是左侧点，那么这里开的就是其确认点，2s 本质上还是1类点
        如果原来是右侧点在，这里就是其2类点

        '''
        BSP_CONF = self.config.GetBSConfig(real_bsp1.is_buy)

        retrace_rate = bsp2_bi.amp()/break_bi.amp()
        bsp2_flag = retrace_rate <= BSP_CONF.max_bs2_rate
        bsp2_flag=1 # 我们这个第二版就不按照回调了，增加二点的数量
        if bsp2_flag and real_bsp1:
            state = 'buy' if real_bsp1.is_buy else 'sell'
            if not left:
                self.add_bs(bs_type=BSP_TYPE.T2, bi=bsp2_bi, relate_bsp1=real_bsp1,reverse=False,is_segbsp=True,zs=zs)  #  这个时候卖点出现在下降笔，所以要reverse
            else:
                self.add_bs(bs_type=BSP_TYPE.T2S, bi=bsp2_bi, relate_bsp1=real_bsp1,is_target_bsp=True,reverse=True,is_segbsp=True,zs=zs)
        elif BSP_CONF.bsp2s_follow_2:
            return


    def treat_bsp2_seg_light_for_class_left1(self, seg: CSeg, real_bsp1, seg_list: CSegListComm[LINE_TYPE],
                             bi_list: LINE_LIST_TYPE, left):
        '''
        二卖的逻辑是一买后面第一个不创新高的顶分，（不创新高，是指从bsp1_bi 这一笔的末尾笔，也就是一买之前的那个顶分开始往后算）
        如果出现了一个顶分，且这个顶分是【是指从bsp1_bi，】非最高点，那就是二买了

        但这里有力度区分，在中枢上，中，还是下，

        也就是上一个一类买卖点的idx 的高点，然后当我我后面形成了线段的顶分，那就是二买了
        也就是idx+1+3+5这些线段的起点，如果能看到什么时候，idx+n的起点不是最高的时候，那颗时刻就是了

        '''

        if not self.seg_need_cal(seg):
            return
        if len(seg_list) > 1:
            BSP_CONF = self.config.GetBSConfig(seg.is_down())
            bsp1_bi = seg.start_bi  # 由于这个是一买之后3笔才是当前时刻，所以这里要用最后一个seg（一买开始的seg）的startbi
            bsp1_bi_idx = bsp1_bi.idx   ## 因为我们的一类点都是延迟确认的，而确认一类买点的时候，其实买点那一笔没有走完，所以这个笔往往比，实际的早1笔。而实际二类买点会在一买后面最少2 笔，

            if bsp1_bi.idx >= len(bi_list):  # 因为我们的一类点都是延迟确认的，因为一买出现的笔滞后了，二买是一买出现后再回调，所以间隔为2
                return
            break_bi = bi_list[bsp1_bi.idx ]
            bsp2_bi = bi_list[bsp1_bi.idx+1]
        else:
            BSP_CONF = self.config.GetBSConfig(seg.is_up())
            bsp1_bi, real_bsp1 = None, None
            bsp1_bi_idx = -1
            if len(bi_list) == 1:
                return
            bsp2_bi = bi_list[1]
            break_bi = bi_list[0]
        if BSP_CONF.bsp2_follow_1 and bsp1_bi_idx not in [bsp.bi.idx for bsp in
                                                          self.bsp_dict.values()]:  # check bsp2_follow_1
            return
        retrace_rate = bsp2_bi.amp() / break_bi.amp()
        bsp2_flag = retrace_rate <= BSP_CONF.max_bs2_rate
        bsp2_flag=1
        if bsp2_flag and real_bsp1:
            state = 'buy' if real_bsp1.is_buy else 'sell'
            if not left:
                self.add_bs(bs_type=BSP_TYPE.T2, bi=bsp2_bi, relate_bsp1=real_bsp1, reverse=True,
                            is_segbsp=True)  # 这个时候卖点出现在下降笔，所以要reverse
            else:
                self.add_bs(bs_type=BSP_TYPE.T2S, bi=bsp2_bi, relate_bsp1=real_bsp1, is_target_bsp=True,
                            reverse=True, is_segbsp=True)
        elif BSP_CONF.bsp2s_follow_2:
            return
        if BSP_TYPE.T2S not in self.config.GetBSConfig(seg.is_down()).target_types:  # 这里的2s 其实就是在左侧的一类买点
            return




    def treat_bsp2(self, seg: CSeg, bsp1_bi_idx_dict, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        '''
        二卖的逻辑是一买后面第一个不创新高的顶分，（不创新高，是指从bsp1_bi 这一笔的末尾笔，也就是一买之前的那个顶分开始往后算）
        如果出现了一个顶分，且这个顶分是【是指从bsp1_bi，】非最高点，那就是二买了

        但这里有力度区分，在中枢上，中，还是下，

        也就是上一个一类买卖点的idx 的高点，然后当我我后面形成了线段的顶分，那就是二买了
        也就是idx+1+3+5这些线段的起点，如果能看到什么时候，idx+n的起点不是最高的时候，那颗时刻就是了

        '''

        if not self.seg_need_cal(seg):
            return
        if len(seg_list) > 1:
            BSP_CONF = self.config.GetBSConfig(seg.is_down())
            bsp1_bi = seg.end_bi
            bsp1_bi_idx = bsp1_bi.idx  ## 因为我们的一类点都是延迟确认的，而确认一类买点的时候，其实买点那一笔没有走完，所以这个笔往往比，实际的早1笔。而实际二类买点会在一买后面最少2 笔，
            real_bsp1 = bsp1_bi_idx_dict.get(bsp1_bi.idx)
            if bsp1_bi.idx + 2  >= len(bi_list):  # 因为我们的一类点都是延迟确认的，因为一买出现的笔滞后了，二买是一买出现后再回调，所以间隔为2
                return
            break_bi = bi_list[bsp1_bi.idx + 1]
            bsp2_bi = bi_list[bsp1_bi.idx + 2]
        else:
            BSP_CONF = self.config.GetBSConfig(seg.is_up())
            bsp1_bi, real_bsp1 = None, None
            bsp1_bi_idx = -1
            if len(bi_list) == 1:
                return
            bsp2_bi = bi_list[1]
            break_bi = bi_list[0]
        if BSP_CONF.bsp2_follow_1 and bsp1_bi_idx not in [bsp.bi.idx for bsp in self.bsp_dict.values()]:  # check bsp2_follow_1
            return
        retrace_rate = bsp2_bi.amp()/break_bi.amp()
        bsp2_flag = retrace_rate <= BSP_CONF.max_bs2_rate
        if bsp2_flag:
            self.add_bs(bs_type=BSP_TYPE.T2, bi=bsp2_bi, relate_bsp1=real_bsp1)  # type: ignore
        elif BSP_CONF.bsp2s_follow_2:
            return
        if BSP_TYPE.T2S not in self.config.GetBSConfig(seg.is_down()).target_types:
            return
        self.treat_bsp2s(seg_list, bi_list, bsp2_bi, break_bi, real_bsp1, BSP_CONF)  # type: ignore

    def treat_bsp2s(
        self,
        seg_list: CSegListComm,
        bi_list: LINE_LIST_TYPE,
        bsp2_bi: LINE_TYPE,
        break_bi: LINE_TYPE,
        real_bsp1: Optional[CBS_Point],
        BSP_CONF: CPointConfig,
    ):
        bias = 2
        _low, _high = None, None
        while bsp2_bi.idx + bias < len(bi_list):  # 计算类二
            bsp2s_bi = bi_list[bsp2_bi.idx + bias]
            assert bsp2s_bi.seg_idx is not None and bsp2_bi.seg_idx is not None
            if BSP_CONF.max_bsp2s_lv is not None and bias/2 > BSP_CONF.max_bsp2s_lv:
                break
            if bsp2s_bi.seg_idx != bsp2_bi.seg_idx and (bsp2s_bi.seg_idx < len(seg_list)-1 or bsp2s_bi.seg_idx - bsp2_bi.seg_idx >= 2 or seg_list[bsp2_bi.seg_idx].is_sure):
                break
            if bias == 2:
                if not has_overlap(bsp2_bi._low(), bsp2_bi._high(), bsp2s_bi._low(), bsp2s_bi._high()):
                    break
                _low = max([bsp2_bi._low(), bsp2s_bi._low()])
                _high = min([bsp2_bi._high(), bsp2s_bi._high()])
            elif not has_overlap(_low, _high, bsp2s_bi._low(), bsp2s_bi._high()):
                break

            if bsp2s_break_bsp1(bsp2s_bi, break_bi):
                break
            retrace_rate = abs(bsp2s_bi.get_end_val()-break_bi.get_end_val())/break_bi.amp()
            if retrace_rate > BSP_CONF.max_bs2_rate:
                break

            self.add_bs(bs_type=BSP_TYPE.T2S, bi=bsp2s_bi, relate_bsp1=real_bsp1)  # type: ignore
            bias += 2


    def cal_seg_bs3point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        bsp1_bi_idx_dict = {bsp.bi.idx: bsp for bsp in self.bsp1o_lst}

        if len( bsp1_bi_idx_dict)<1 or len(seg_list) < 1:
            return

        bsp=self.bsp1o_lst[-1]
        newest_bsp1_bi_idx_dict = {bsp.bi.idx: bsp }

        bsp1_bi=bsp.bi.idx
        newest_bi=bi_list[-1].idx

        bsp1_seg_idx=bsp.bi.seg_idx # bsp1 属于哪一个大的seg，中枢是这个seg里面的最后一个

        if len(seg_list)<=bsp1_seg_idx:
            return



        if len(seg_list[bsp1_seg_idx].zs_lst)==0:
            return

        #zs= seg_list[bsp1_seg_idx].zs_lst[-1]  # 我们在有了one bi zs之后，其实最后一个中枢不一定是最后一个中枢，要看bsp 中的bi(bi out)在哪一个后面
        zs = find_czs_with_target_idx(seg_list[bsp1_seg_idx].zs_lst,bsp.bi.idx)
        if zs is None: # 找不到的情况下再用这个
            zs = seg_list[bsp1_seg_idx].zs_lst[-1]


        # if len(seg_list)-1>bsp1_seg_idx+2: # 一买时2的最后，那么现在就在3 这个seg上， len最大是4 否则就是去了机会
        #     return

        # Create a list to hold every bsp1_bi + even number (2, 4, 6, ...) until it is >= newest_bi
        idx_list = []
        current_bi = bsp1_bi+3
        while current_bi <= newest_bi:
            idx_list.append(current_bi)
            current_bi += 2

        if len(idx_list) < 2:
            return

        # if not bi_list[-3].is_sure:  # 这个顶的前2笔要是确定的
        #     return

        if len(self.bsp3_lst)>=1:
            # 获取 bsp1o_lst 和 bsp2_lst 中的最后一个 bsp 对象
            bsp1_last = self.bsp1o_lst[-1]
            bsp3_last = self.bsp3_lst[-1]
            # 获取最后一个点的时间
            bsp1_last_time = bsp1_last.klu.time
            bsp3_last_time = bsp3_last.klu.time

            if bsp1_last_time <= bsp3_last_time:
                # print('已经出现了第二类买点，后续同类型跳过')
                return


        # Extract the high values corresponding to the selected indices
        peak_points = []
        for idx in idx_list:
            if idx < len(bi_list):
                if bsp.is_buy:
                    peak_points.append(bi_list[idx].start_bi.begin_klc.low)
                else:
                    peak_points.append(bi_list[idx].start_bi.begin_klc.high)

        # Compare the latest high point with the maximum high point in the list
        if peak_points:
            latest_high_point = peak_points[-1]
            if bsp.is_buy:
                bsp3=latest_high_point>zs.high
            else:
                bsp3=latest_high_point<zs.low

            if bsp.is_buy:
                strong_bsp3=latest_high_point>zs.peak_high
            else:
                strong_bsp3=latest_high_point<zs.peak_low

            # Check if the latest high point is NOT the maximum value
            if bsp3 and not strong_bsp3:
                real_bsp1 = bsp1_bi_idx_dict.get(bsp.bi.idx)
                bsp3_bi=bi_list[idx]
                is_buy = not bsp3_bi.is_down()
                if is_buy==bsp.is_buy: # 这个的方向必须与最近的一个一类点的方向一致
                    self.add_bs3(bs_type=BSP_TYPE.T3A, bi=bsp3_bi, relate_bsp1=real_bsp1,reverse=True)  # type: ignore
                    print('出现3类买卖点')
            if  strong_bsp3:
                real_bsp1 = bsp1_bi_idx_dict.get(bsp.bi.idx)
                bsp3_bi = bi_list[idx]

                is_buy = not bsp3_bi.is_down()
                if is_buy==bsp.is_buy: # 这个的方向必须与最近的一个一类点的方向一致

                    self.add_bs3(bs_type=BSP_TYPE.T3B, bi=bsp3_bi, relate_bsp1=real_bsp1,reverse=True)  # type: ignore
                    print('出现强3类买卖点')




                return True  # The latest high point is not the highest
            else:
                return False  # The latest high point is the highest
        return False




        # for bsp in self.bsp1o_lst:
        # for seg in seg_list:
        #     config = self.config.GetBSConfig(seg.is_down())
        #     if BSP_TYPE.T2 not in config.target_types and BSP_TYPE.T2S not in config.target_types:
        #         continue
        #     self.treat_bsp2(seg, bsp1_bi_idx_dict, seg_list, bi_list)
    def cal_seg_bs3point_1leftsure(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        bsp1_bi_idx_dict = {bsp.bi.idx: bsp for bsp in self.bsp1_seg_lst}

        if len(bsp1_bi_idx_dict) < 1 or len(seg_list) < 1:
            return

        # Find the last buy point and last sell point from bsp1_seg_left_makesure_lst
        last_buy_point = None
        last_sell_point = None

        for point in reversed(self.bsp1_seg_lst):
            if (point.is_buy and point.left_right=='left') and last_buy_point is None:
                last_buy_point = point
            elif (not point.is_buy and point.left_right=='left')and last_sell_point is None:
                last_sell_point = point
            if last_buy_point is not None and last_sell_point is not None:
                break

        # Latest third type points
        last_buy_point3 = None
        last_sell_point3 = None

        for point in reversed(self.bsp3_lst):
            if point.is_buy and last_buy_point3 is None:
                last_buy_point3 = point
            elif not point.is_buy and last_sell_point3 is None:
                last_sell_point3 = point
            if last_buy_point3 is not None and last_sell_point3 is not None:
                break

        def process_bsp(last_point, last_point3):
            if last_point is None:
                return

            bsp = last_point
            bsp1_bi = bsp.bi.idx
            newest_bi = bi_list[-1].idx
            bsp1_seg_idx = bsp.bi.seg_idx

            if len(seg_list) <= bsp1_seg_idx:
                return

            if len(seg_list[bsp1_seg_idx].zs_lst) == 0:
                return

            zs = find_czs_with_target_idx(seg_list[bsp1_seg_idx].zs_lst, bsp.bi.idx)
            if zs is None:
                zs = seg_list[bsp1_seg_idx].zs_lst[-1]

            # 如果sell 就去bsp之后的向下笔
            idx_list = []

            for seg_idx in range(bsp1_bi, newest_bi+1):
                if bi_list[seg_idx].is_down():
                    if not bsp.is_buy and bsp.bi.dir == BI_DIR.UP: # 取后面的向下笔
                                idx_list.append(seg_idx)
                    if not bsp.is_buy and bsp.bi.dir == BI_DIR.DOWN: # 取后面的向下笔
                                idx_list.append(seg_idx)
                else: #卖点看向上的笔，这时候的底才是走完的底
                    if  bsp.is_buy and bsp.bi.dir == BI_DIR.UP: # 取后面的向下笔
                                idx_list.append(seg_idx)
                    if  bsp.is_buy and bsp.bi.dir == BI_DIR.DOWN: # 取后面的向下笔
                                idx_list.append(seg_idx)



            if len(idx_list) < 1:
                return



            if last_point3 is not None:
                bsp1_last_time = last_point.klu.time
                bsp3_last_time = last_point3.klu.time

                if bsp1_last_time <= bsp3_last_time:
                    return

            peak_points = []
            for idx in idx_list:
                if idx < len(bi_list):
                    if bsp.is_buy:
                        peak_points.append(bi_list[idx].start_bi.begin_klc.low)
                    else:
                        peak_points.append(bi_list[idx].start_bi.begin_klc.high)

            if len(bi_list[idx_list[-1]].bi_list)<3:
                return


            if peak_points:
                latest_high_point = peak_points[-1]
                if bsp.is_buy:
                    bsp3 = latest_high_point > zs.high
                else:
                    bsp3 = latest_high_point < zs.low

                if bsp.is_buy:
                    strong_bsp3 = latest_high_point > zs.peak_high
                else:
                    strong_bsp3 = latest_high_point < zs.peak_low

                if bsp3 and not strong_bsp3:
                    real_bsp1 = bsp1_bi_idx_dict.get(bsp.bi.idx)
                    bsp3_bi = bi_list[idx]
                    is_buy = not bsp3_bi.is_down()
                    if is_buy == bsp.is_buy:
                        self.add_bs3(bs_type=BSP_TYPE.T3C, bi=bsp3_bi, relate_bsp1=real_bsp1,
                                     reverse=True)  # type: ignore
                        print('出现3left买卖点')

                if strong_bsp3:
                    real_bsp1 = bsp1_bi_idx_dict.get(bsp.bi.idx)
                    bsp3_bi = bi_list[idx]
                    is_buy = not bsp3_bi.is_down()
                    if is_buy == bsp.is_buy:
                        self.add_bs3(bs_type=BSP_TYPE.T3D, bi=bsp3_bi, relate_bsp1=real_bsp1,
                                     reverse=True)  # type: ignore
                        print('出现强3left买卖点')
                    return True
                else:
                    return False
            return False

        # Process for both buy and sell points
        process_bsp(last_buy_point, last_buy_point3)
        process_bsp(last_sell_point, last_sell_point3)
    def cal_seg_bs3point_1left(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        bsp1_bi_idx_dict = {bsp.bi.idx: bsp for bsp in self.bsp1_seg_lst}

        if len(bsp1_bi_idx_dict) < 1 or len(seg_list) < 1:
            return

        # Find the last buy point and last sell point from bsp1_seg_left_makesure_lst
        last_buy_point = None
        last_sell_point = None

        for point in reversed(self.bsp1_seg_lst):
            if (point.is_buy and point.left_right=='left') and last_buy_point is None:
                last_buy_point = point
            elif (not point.is_buy and point.left_right=='left')and last_sell_point is None:
                last_sell_point = point
            if last_buy_point is not None and last_sell_point is not None:
                break

        # Latest third type points
        last_buy_point3 = None
        last_sell_point3 = None

        for point in reversed(self.bsp3_lst):
            if point.is_buy and last_buy_point3 is None:
                last_buy_point3 = point
            elif not point.is_buy and last_sell_point3 is None:
                last_sell_point3 = point
            if last_buy_point3 is not None and last_sell_point3 is not None:
                break

        def process_bsp(last_point, last_point3):
            if last_point is None:
                return

            bsp = last_point
            bsp1_bi = bsp.bi.idx
            newest_bi = bi_list[-1].idx
            bsp1_seg_idx = bsp.bi.seg_idx

            if len(seg_list) <= bsp1_seg_idx:
                return

            if len(seg_list[bsp1_seg_idx].zs_lst) == 0:
                return

            zs = find_czs_with_target_idx(seg_list[bsp1_seg_idx].zs_lst, bsp.bi.idx)
            if zs is None:
                zs = seg_list[bsp1_seg_idx].zs_lst[-1]

            idx_list = []
            current_bi = bsp1_bi + 3
            while current_bi <= newest_bi:
                idx_list.append(current_bi)
                current_bi += 2

            if len(idx_list) < 2:
                return

            # if not bi_list[-3].is_sure:
            #     return

            if last_point3 is not None:
                bsp1_last_time = last_point.klu.time
                bsp3_last_time = last_point3.klu.time

                if bsp1_last_time <= bsp3_last_time:
                    return

            peak_points = []
            for idx in idx_list:
                if idx < len(bi_list):
                    if bsp.is_buy:
                        peak_points.append(bi_list[idx].start_bi.begin_klc.low)
                    else:
                        peak_points.append(bi_list[idx].start_bi.begin_klc.high)

            if peak_points:
                latest_high_point = peak_points[-1]
                if bsp.is_buy:
                    bsp3 = latest_high_point > zs.high
                else:
                    bsp3 = latest_high_point < zs.low

                if bsp.is_buy:
                    strong_bsp3 = latest_high_point > zs.peak_high
                else:
                    strong_bsp3 = latest_high_point < zs.peak_low

                if bsp3 and not strong_bsp3:
                    real_bsp1 = bsp1_bi_idx_dict.get(bsp.bi.idx)
                    bsp3_bi = bi_list[idx]
                    is_buy = not bsp3_bi.is_down()
                    if is_buy == bsp.is_buy:
                        self.add_bs3(bs_type=BSP_TYPE.T3LC, bi=bsp3_bi, relate_bsp1=real_bsp1,
                                     reverse=True)  # type: ignore
                        print('出现3left买卖点')

                if strong_bsp3:
                    real_bsp1 = bsp1_bi_idx_dict.get(bsp.bi.idx)
                    bsp3_bi = bi_list[idx]
                    is_buy = not bsp3_bi.is_down()
                    if is_buy == bsp.is_buy:
                        self.add_bs3(bs_type=BSP_TYPE.T3LD, bi=bsp3_bi, relate_bsp1=real_bsp1,
                                     reverse=True)  # type: ignore
                        print('出现强3left买卖点')
                    return True
                else:
                    return False
            return False

        # Process for both buy and sell points
        process_bsp(last_buy_point, last_buy_point3)
        process_bsp(last_sell_point, last_sell_point3)
    def cal_seg_bs3point_1right(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        bsp1_bi_idx_dict = {bsp.bi.idx: bsp for bsp in self.bsp1_seg_lst}

        if len(bsp1_bi_idx_dict) < 1 or len(seg_list) < 1:
            return

        # Find the last buy point and last sell point from bsp1_seg_left_makesure_lst
        last_buy_point = None
        last_sell_point = None

        for point in reversed(self.bsp1_seg_lst):
            if (point.is_buy and point.left_right=='right') and last_buy_point is None:
                last_buy_point = point
            elif (not point.is_buy and point.left_right=='right')and last_sell_point is None:
                last_sell_point = point
            if last_buy_point is not None and last_sell_point is not None:
                break

        # Latest third type points
        last_buy_point3 = None
        last_sell_point3 = None

        for point in reversed(self.bsp3_lst):
            if point.is_buy and last_buy_point3 is None:
                last_buy_point3 = point
            elif not point.is_buy and last_sell_point3 is None:
                last_sell_point3 = point
            if last_buy_point3 is not None and last_sell_point3 is not None:
                break

        def process_bsp(last_point, last_point3):
            if last_point is None:
                return

            bsp = last_point
            bsp1_bi = bsp.bi.idx
            newest_bi = bi_list[-1].idx
            bsp1_seg_idx = bsp.bi.seg_idx

            if len(seg_list) <= bsp1_seg_idx:
                return

            if len(seg_list[bsp1_seg_idx].zs_lst) == 0:
                return

            zs = find_czs_with_target_idx(seg_list[bsp1_seg_idx].zs_lst, bsp.bi.idx)
            if zs is None:
                zs = seg_list[bsp1_seg_idx].zs_lst[-1]

            idx_list = []
            current_bi = bsp1_bi + 3
            while current_bi <= newest_bi:
                idx_list.append(current_bi)
                current_bi += 2

            if len(idx_list) < 2:
                return

            # if not bi_list[-3].is_sure:
            #     return

            if last_point3 is not None:
                bsp1_last_time = last_point.klu.time
                bsp3_last_time = last_point3.klu.time

                if bsp1_last_time <= bsp3_last_time:
                    return

            peak_points = []
            for idx in idx_list:
                if idx < len(bi_list):
                    if bsp.is_buy:
                        peak_points.append(bi_list[idx].start_bi.begin_klc.low)
                    else:
                        peak_points.append(bi_list[idx].start_bi.begin_klc.high)

            if peak_points:
                latest_high_point = peak_points[-1]
                if bsp.is_buy:
                    bsp3 = latest_high_point > zs.high
                else:
                    bsp3 = latest_high_point < zs.low

                if bsp.is_buy:
                    strong_bsp3 = latest_high_point > zs.peak_high
                else:
                    strong_bsp3 = latest_high_point < zs.peak_low

                if bsp3 and not strong_bsp3:
                    real_bsp1 = bsp1_bi_idx_dict.get(bsp.bi.idx)
                    bsp3_bi = bi_list[idx]
                    is_buy = not bsp3_bi.is_down()
                    if is_buy == bsp.is_buy:
                        self.add_bs3(bs_type=BSP_TYPE.T3RC, bi=bsp3_bi, relate_bsp1=real_bsp1,
                                     reverse=True)  # type: ignore
                        print('出现3right买卖点')

                if strong_bsp3:
                    real_bsp1 = bsp1_bi_idx_dict.get(bsp.bi.idx)
                    bsp3_bi = bi_list[idx]
                    is_buy = not bsp3_bi.is_down()
                    if is_buy == bsp.is_buy:
                        self.add_bs3(bs_type=BSP_TYPE.T3RD, bi=bsp3_bi, relate_bsp1=real_bsp1,
                                     reverse=True)  # type: ignore
                        print('出现强3right买卖点')
                    return True
                else:
                    return False
            return False

        # Process for both buy and sell points
        process_bsp(last_buy_point, last_buy_point3)
        process_bsp(last_sell_point, last_sell_point3)

    def cal_seg_bs3point_old(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE):
        bsp1_bi_idx_dict = {bsp.bi.idx: bsp for bsp in self.bsp1o_lst}
        for seg in seg_list:
            if not self.seg_need_cal(seg):
                continue
            config = self.config.GetBSConfig(seg.is_down())
            if BSP_TYPE.T3A not in config.target_types and BSP_TYPE.T3B not in config.target_types:
                continue
            if len(seg_list) > 1:
                bsp1_bi = seg.end_bi
                bsp1_bi_idx = bsp1_bi.idx
                BSP_CONF = self.config.GetBSConfig(seg.is_down())
                real_bsp1 = bsp1_bi_idx_dict.get(bsp1_bi.idx)
                next_seg_idx = seg.idx+1
                next_seg = seg.next  # 可能为None, 所以并不一定可以保证next_seg_idx == next_seg.idx
            else:
                next_seg = seg
                next_seg_idx = seg.idx
                bsp1_bi, real_bsp1 = None, None
                bsp1_bi_idx = -1
                BSP_CONF = self.config.GetBSConfig(seg.is_up())
            if BSP_CONF.bsp3_follow_1 and bsp1_bi_idx not in [bsp.bi.idx for bsp in self.bsp_dict.values()]:
                continue
            if next_seg:
                self.treat_bsp3_after(seg_list, next_seg, BSP_CONF, bi_list, real_bsp1, bsp1_bi_idx, next_seg_idx)
            self.treat_bsp3_before(seg_list, seg, next_seg, bsp1_bi, BSP_CONF, bi_list, real_bsp1, next_seg_idx)


    def cal_seg_bs4point(self, seg_list: CSegListComm[LINE_TYPE], bi_list: LINE_LIST_TYPE,detail_bi_list):
        '''
        seg_list 其实是 segseg，也就是线段上一级别
        bi_list 其实是线段
        '''

        last_bsp4=self.bsp4_lst[-1] if self.bsp4_lst else None

        if len(seg_list) < 2:
            return
        current_segseg=seg_list[-1]
        last_segseg = seg_list[-2]

        if len(last_segseg.zs_lst)<1:
            return

        # 找到一类点，看看是不是要进行补偿
        if len(self.bsp1o_lst)<1 or len(self.bsp1_seg_lst)<1:
            return
        # 判断上一个seg的点是不是在第一个中枢之后
        last_1_points=self.bsp1_seg_lst[-1] if self.bsp1_seg_lst[-1].klu.time>last_segseg.zs_lst[-1].end.time else None
        last_1o_points = self.bsp1o_lst[-1] if self.bsp1o_lst[-1].klu.time>last_segseg.zs_lst[-1].end.time else None

        # 检测有没有补偿
        if last_bsp4 is not None and last_bsp4.relate_bsp1==last_1_points:
            return

        if last_1_points is None or last_1o_points is None:
            return
        if last_1o_points.make_sure_time is None:
            return

        peak_time=last_segseg.bi_list[-1].end_bi.end_klc.time_end.datetime
        early_time = min(last_1_points.klu.time.datetime, last_1o_points.make_sure_time.datetime)
        late_time = max(last_1_points.klu.time.datetime, last_1o_points.make_sure_time.datetime)

        peak_seg_idx=last_segseg.bi_list[-1].idx


        if last_1_points.is_buy==last_1o_points.is_buy==True and early_time<peak_time:
            if late_time>peak_time:


                if last_1_points.klu.time.datetime<last_1o_points.make_sure_time.datetime:
                    high_price = last_1_points.klu.close
                    buy_seg_idx=last_1_points.bi.idx
                    # low_price = last_1o_points.make_sure_klu.close
                else:
                    high_price = last_1o_points.make_sure_klu.close
                    buy_seg_idx= last_1o_points.bi.idx
                    # low_price=last_1_points.klu.close

            else:
                # 去右边的点
                if last_1_points.klu.time.datetime<last_1o_points.make_sure_time.datetime:
                    high_price = last_1o_points.make_sure_klu.close
                    buy_seg_idx = last_1o_points.bi.idx
                    # low_price = last_1_points.klu.close

                else:
                    high_price = last_1_points.klu.close
                    buy_seg_idx = last_1_points.bi.idx
                    # low_price = last_1o_points.make_sure_klu.close




            if peak_seg_idx-buy_seg_idx>1:
                return

            point_buy=True
            need_buchang=(last_segseg.bi_list[-1].end_bi.end_klc.low-high_price)/high_price<-0.015

        elif last_1_points.is_buy==last_1o_points.is_buy==False and early_time<peak_time:

            if late_time > peak_time:

                if last_1_points.klu.time.datetime < last_1o_points.make_sure_time.datetime:
                    high_price = last_1_points.klu.close
                    buy_seg_idx = last_1_points.bi.idx
                    # low_price = last_1o_points.make_sure_klu.close
                else:
                    high_price = last_1o_points.make_sure_klu.close
                    buy_seg_idx = last_1o_points.bi.idx
                    # low_price = last_1_points.make_sure_klu.close
            else:

                if last_1_points.klu.time.datetime < last_1o_points.make_sure_time.datetime:
                    high_price = last_1o_points.make_sure_klu.close
                    buy_seg_idx = last_1o_points.bi.idx
                    # low_price = last_1_points.make_sure_klu.close

                else:
                    high_price = last_1_points.klu.close
                    buy_seg_idx = last_1_points.bi.idx
                    # low_price = last_1o_points.make_sure_klu.close



            if peak_seg_idx-buy_seg_idx>1:
                return
            point_buy = False
            need_buchang =(last_segseg.bi_list[-1].end_bi.end_klc.high - high_price) / high_price > 0.015
        else:
            return

        if not need_buchang:
            return
        # 判断是两个中枢还是跨级别中枢
        # if last_segseg.zs_lst[-1].bi_lst[-1].idx >= current_segseg.bi_list[0].idx:
        #     print('跨级别中枢形成')


        Last_bi=detail_bi_list[-1]

        if current_segseg.dir==BI_DIR.UP and point_buy:
            if len(current_segseg.bi_list[0].zs_lst) > 0 : #and Last_bi.end_klc.high>low_price
            # if last_segseg.bi_list[-1].bi_list[-1].is_sure :
            # if Last_bi.begin_klc.low>last_segseg.zs_lst[-1].low:
                print('跨级别中枢，buy 的一类补偿点')
                self.add_bs(bs_type=BSP_TYPE.T4, bi=current_segseg, relate_bsp1=last_1_points,reverse=True,is_target_bsp=True,is_segbsp=True)  # type: ignore
        if current_segseg.dir==BI_DIR.DOWN and not point_buy:
            if len(current_segseg.bi_list[0].zs_lst)>0 :  #and Last_bi.end_klc.low<low_price
            # if last_segseg.bi_list[-1].bi_list[-1].is_sure:
            # if Last_bi.begin_klc.high<last_segseg.zs_lst[-1].high:
                print('跨级别中枢，sell的一类补偿点')
                self.add_bs(bs_type=BSP_TYPE.T4, bi=current_segseg, relate_bsp1=last_1_points, reverse=True, is_target_bsp=True,
                            is_segbsp=True)







    def treat_bsp3_after(
        self,
        seg_list: CSegListComm[LINE_TYPE],
        next_seg: CSeg[LINE_TYPE],
        BSP_CONF: CPointConfig,
        bi_list: LINE_LIST_TYPE,
        real_bsp1,
        bsp1_bi_idx,
        next_seg_idx
    ):
        first_zs = next_seg.get_first_multi_bi_zs()
        if first_zs is None:
            return
        if BSP_CONF.strict_bsp3 and first_zs.get_bi_in().idx != bsp1_bi_idx+1:
            return
        if first_zs.bi_out is None or first_zs.bi_out.idx+1 >= len(bi_list):
            return
        bsp3_bi = bi_list[first_zs.bi_out.idx+1]
        if bsp3_bi.parent_seg is None:
            if next_seg.idx != len(seg_list)-1:
                return
        elif bsp3_bi.parent_seg.idx != next_seg.idx:
            if len(bsp3_bi.parent_seg.bi_list) >= 3:
                return
        if bsp3_bi.dir == next_seg.dir:
            return
        if bsp3_bi.seg_idx != next_seg_idx and next_seg_idx < len(seg_list)-2:
            return
        if bsp3_back2zs(bsp3_bi, first_zs):
            return
        bsp3_peak_zs = bsp3_break_zspeak(bsp3_bi, first_zs)
        if BSP_CONF.bsp3_peak and not bsp3_peak_zs:
            return
        print('add bsp3 after:', bsp3_bi)
        self.add_bs(bs_type=BSP_TYPE.T3A, bi=bsp3_bi, relate_bsp1=real_bsp1)  # type: ignore

    def treat_bsp3_before(
        self,
        seg_list: CSegListComm[LINE_TYPE],
        seg: CSeg[LINE_TYPE],
        next_seg: Optional[CSeg[LINE_TYPE]],
        bsp1_bi: Optional[LINE_TYPE],
        BSP_CONF: CPointConfig,
        bi_list: LINE_LIST_TYPE,
        real_bsp1,
        next_seg_idx
    ):
        cmp_zs = seg.get_final_multi_bi_zs()
        if cmp_zs is None:
            return
        if not bsp1_bi:
            return
        if BSP_CONF.strict_bsp3 and (cmp_zs.bi_out is None or cmp_zs.bi_out.idx != bsp1_bi.idx):
            return
        end_bi_idx = cal_bsp3_bi_end_idx(next_seg)
        for bsp3_bi in bi_list[bsp1_bi.idx+2::2]:
            if bsp3_bi.idx > end_bi_idx:
                break
            assert bsp3_bi.seg_idx is not None
            if bsp3_bi.seg_idx != next_seg_idx and bsp3_bi.seg_idx < len(seg_list)-1:
                break
            if bsp3_back2zs(bsp3_bi, cmp_zs):  # type: ignore
                continue
            print('add bsp3 before:', bsp3_bi)
            self.add_bs(bs_type=BSP_TYPE.T3B, bi=bsp3_bi, relate_bsp1=real_bsp1)  # type: ignore
            break

    def getLastestBspList(self) -> List[CBS_Point[LINE_TYPE]]:
        if len(self.lst) == 0:
            return []
        return sorted(self.lst, key=lambda bsp: bsp.bi.idx, reverse=True)


def bsp2s_break_bsp1(bsp2s_bi: LINE_TYPE, bsp2_break_bi: LINE_TYPE) -> bool:
    return (bsp2s_bi.is_down() and bsp2s_bi._low() < bsp2_break_bi._low()) or \
           (bsp2s_bi.is_up() and bsp2s_bi._high() > bsp2_break_bi._high())


def bsp3_back2zs(bsp3_bi: LINE_TYPE, zs: CZS) -> bool:
    return (bsp3_bi.is_down() and bsp3_bi._low() < zs.high) or (bsp3_bi.is_up() and bsp3_bi._high() > zs.low)


def bsp3_break_zspeak(bsp3_bi: LINE_TYPE, zs: CZS) -> bool:
    return (bsp3_bi.is_down() and bsp3_bi._high() >= zs.peak_high) or (bsp3_bi.is_up() and bsp3_bi._low() <= zs.peak_low)


def cal_bsp3_bi_end_idx(seg: Optional[CSeg[LINE_TYPE]]):
    if not seg:
        return float("inf")
    if seg.get_multi_bi_zs_cnt() == 0 and seg.next is None:
        return float("inf")
    end_bi_idx = seg.end_bi.idx-1
    for zs in seg.zs_lst:
        if zs.is_one_bi_zs():
            continue
        if zs.bi_out is not None:
            end_bi_idx = zs.bi_out.idx
            break
    return end_bi_idx
