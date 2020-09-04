import pandas as pd
from freqtrade.exchange import timeframe_to_minutes


def merge_informative_pairs(dataframe: pd.DataFrame, informative: pd.DataFrame,
                            timeframe_inf: str, ffill: bool = True) -> pd.DataFrame:
    """
    Correctly merge informative samples to the original dataframe, avoiding lookahead bias.

    Since dates are candle open dates, merging a 15m candle that starts at 15:00, and a
    1h candle that starts at 15:00 will result in all candles to know the close at 16:00
    which they should not know.

    Moves the date of the informative pair by 1 time interval forward.
    This way, the 14:00 1h candle is merged to 15:00 15m candle, since the 14:00 1h candle is the
    last candle that's closed at 15:00, 15:15, 15:30 or 15:45.

    :param dataframe: Original dataframe
    :param informative: Informative pair, most likely loaded via dp.get_pair_dataframe
    :param timeframe_inf: Timeframe of the informative pair sample.
    :param ffill: Forwardfill missing values - optional but usually required
    """
    # Rename columns to be unique

    minutes = timeframe_to_minutes(timeframe_inf)
    informative['date_merge'] = informative["date"] + pd.to_timedelta(minutes, 'm')

    informative.columns = [f"{col}_{timeframe_inf}" for col in informative.columns]

    # Combine the 2 dataframes
    # all indicators on the informative sample MUST be calculated before this point
    dataframe = pd.merge(dataframe, informative, left_on='date',
                         right_on=f'date_merge_{timeframe_inf}', how='left')
    dataframe = dataframe.drop(f'date_merge_{timeframe_inf}', axis=1)

    if ffill:
        dataframe = dataframe.ffill()

    return dataframe
