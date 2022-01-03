# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement

from datetime import datetime

import talib.abstract as ta
from pandas import DataFrame

import freqtrade.vendor.qtpylib.indicators as qtpylib
from freqtrade.strategy import (BooleanParameter, DecimalParameter, IntParameter, IStrategy,
                                RealParameter)


class StrategyTestV3(IStrategy):
    """
    Strategy used by tests freqtrade bot.
    Please do not modify this strategy, it's  intended for internal use only.
    Please look at the SampleStrategy in the user_data/strategy directory
    or strategy repository https://github.com/freqtrade/freqtrade-strategies
    for samples and inspiration.
    """
    INTERFACE_VERSION = 3

    # Minimal ROI designed for the strategy
    minimal_roi = {
        "40": 0.0,
        "30": 0.01,
        "20": 0.02,
        "0": 0.04
    }

    # Optimal stoploss designed for the strategy
    stoploss = -0.10

    # Optimal timeframe for the strategy
    timeframe = '5m'

    # Optional order type mapping
    order_types = {
        'buy': 'limit',
        'sell': 'limit',
        'stoploss': 'limit',
        'stoploss_on_exchange': False
    }

    # Number of candles the strategy requires before producing valid signals
    startup_candle_count: int = 20

    # Optional time in force for orders
    order_time_in_force = {
        'buy': 'gtc',
        'sell': 'gtc',
    }

    buy_params = {
        'buy_rsi': 35,
        # Intentionally not specified, so "default" is tested
        # 'buy_plusdi': 0.4
    }

    sell_params = {
        'sell_rsi': 74,
        'sell_minusdi': 0.4
    }

    buy_rsi = IntParameter([0, 50], default=30, space='buy')
    buy_plusdi = RealParameter(low=0, high=1, default=0.5, space='buy')
    sell_rsi = IntParameter(low=50, high=100, default=70, space='sell')
    sell_minusdi = DecimalParameter(low=0, high=1, default=0.5001, decimals=3, space='sell',
                                    load=False)
    protection_enabled = BooleanParameter(default=True)
    protection_cooldown_lookback = IntParameter([0, 50], default=30)

    # TODO-lev: Can this work with protection tests? (replace HyperoptableStrategy implicitly ... )
    # @property
    # def protections(self):
    #     prot = []
    #     if self.protection_enabled.value:
    #         prot.append({
    #             "method": "CooldownPeriod",
    #             "stop_duration_candles": self.protection_cooldown_lookback.value
    #         })
    #     return prot

    def informative_pairs(self):

        return []

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        # Momentum Indicator
        # ------------------------------------

        # ADX
        dataframe['adx'] = ta.ADX(dataframe)

        # MACD
        macd = ta.MACD(dataframe)
        dataframe['macd'] = macd['macd']
        dataframe['macdsignal'] = macd['macdsignal']
        dataframe['macdhist'] = macd['macdhist']

        # Minus Directional Indicator / Movement
        dataframe['minus_di'] = ta.MINUS_DI(dataframe)

        # Plus Directional Indicator / Movement
        dataframe['plus_di'] = ta.PLUS_DI(dataframe)

        # RSI
        dataframe['rsi'] = ta.RSI(dataframe)

        # Stoch fast
        stoch_fast = ta.STOCHF(dataframe)
        dataframe['fastd'] = stoch_fast['fastd']
        dataframe['fastk'] = stoch_fast['fastk']

        # Bollinger bands
        bollinger = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe['bb_lowerband'] = bollinger['lower']
        dataframe['bb_middleband'] = bollinger['mid']
        dataframe['bb_upperband'] = bollinger['upper']

        # EMA - Exponential Moving Average
        dataframe['ema10'] = ta.EMA(dataframe, timeperiod=10)

        return dataframe

    def populate_buy_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        dataframe.loc[
            (
                (dataframe['rsi'] < self.buy_rsi.value) &
                (dataframe['fastd'] < 35) &
                (dataframe['adx'] > 30) &
                (dataframe['plus_di'] > self.buy_plusdi.value)
            ) |
            (
                (dataframe['adx'] > 65) &
                (dataframe['plus_di'] > self.buy_plusdi.value)
            ),
            'enter_long'] = 1
        dataframe.loc[
            (
                qtpylib.crossed_below(dataframe['rsi'], self.sell_rsi.value)
            ),
            'enter_short'] = 1

        return dataframe

    def populate_sell_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (
                    (qtpylib.crossed_above(dataframe['rsi'], self.sell_rsi.value)) |
                    (qtpylib.crossed_above(dataframe['fastd'], 70))
                ) &
                (dataframe['adx'] > 10) &
                (dataframe['minus_di'] > 0)
            ) |
            (
                (dataframe['adx'] > 70) &
                (dataframe['minus_di'] > self.sell_minusdi.value)
            ),
            'exit_long'] = 1

        dataframe.loc[
            (
                qtpylib.crossed_above(dataframe['rsi'], self.buy_rsi.value)
            ),
            'exit_short'] = 1

        # TODO-lev: Add short logic
        return dataframe

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, side: str,
                 **kwargs) -> float:
        # Return 3.0 in all cases.
        # Bot-logic must make sure it's an allowed leverage and eventually adjust accordingly.

        return 3.0
