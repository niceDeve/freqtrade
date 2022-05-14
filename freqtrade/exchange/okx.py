import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import ccxt

from freqtrade.constants import BuySell
from freqtrade.enums import MarginMode, TradingMode
from freqtrade.enums.candletype import CandleType
from freqtrade.exceptions import DDosProtection, OperationalException, TemporaryError
from freqtrade.exchange import Exchange
from freqtrade.exchange.common import retrier
from freqtrade.exchange.exchange import timeframe_to_minutes


logger = logging.getLogger(__name__)


class Okx(Exchange):
    """Okx exchange class.

    Contains adjustments needed for Freqtrade to work with this exchange.
    """

    _ft_has: Dict = {
        "ohlcv_candle_limit": 300,  # Warning, special case with data prior to X months
        "mark_ohlcv_timeframe": "4h",
        "funding_fee_timeframe": "8h",
    }
    _ft_has_futures: Dict = {
        "tickers_have_quoteVolume": False,
    }

    _supported_trading_mode_margin_pairs: List[Tuple[TradingMode, MarginMode]] = [
        # TradingMode.SPOT always supported and not required in this list
        # (TradingMode.MARGIN, MarginMode.CROSS),
        # (TradingMode.FUTURES, MarginMode.CROSS),
        (TradingMode.FUTURES, MarginMode.ISOLATED),
    ]

    net_only = True

    def ohlcv_candle_limit(
                self, timeframe: str, candle_type: CandleType, since_ms: Optional[int] = None) -> int:
        """
        Exchange ohlcv candle limit
        Uses ohlcv_candle_limit_per_timeframe if the exchange has different limits
        per timeframe (e.g. bittrex), otherwise falls back to ohlcv_candle_limit
        :param timeframe: Timeframe to check
        :param candle_type: Candle-type
        :param since_ms: Candle-type
        :return: Candle limit as integer
        """
        now = datetime.now(timezone.utc)
        offset_mins = timeframe_to_minutes(timeframe) * self._ft_has['ohlcv_candle_limit']
        if since_ms and since_ms < ((now - timedelta(minutes=offset_mins)).timestamp() * 1000):
            return 100
        if candle_type not in (CandleType.FUTURES, CandleType.SPOT):
            return 100

        return int(self._ft_has.get('ohlcv_candle_limit_per_timeframe', {}).get(
            timeframe, self._ft_has.get('ohlcv_candle_limit')))

    @retrier
    def additional_exchange_init(self) -> None:
        """
        Additional exchange initialization logic.
        .api will be available at this point.
        Must be overridden in child methods if required.
        """
        try:
            if self.trading_mode == TradingMode.FUTURES and not self._config['dry_run']:
                accounts = self._api.fetch_accounts()
                if len(accounts) > 0:
                    self.net_only = accounts[0].get('info', {}).get('posMode') == 'net_mode'
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f'Could not set leverage due to {e.__class__.__name__}. Message: {e}') from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def _get_posSide(self, side: BuySell, reduceOnly: bool):
        if self.net_only:
            return 'net'
        if not reduceOnly:
            # Enter
            return 'long' if side == 'buy' else 'short'
        else:
            # Exit
            return 'long' if side == 'sell' else 'short'

    def _get_params(
        self,
        side: BuySell,
        ordertype: str,
        leverage: float,
        reduceOnly: bool,
        time_in_force: str = 'gtc',
    ) -> Dict:
        params = super()._get_params(
            side=side,
            ordertype=ordertype,
            leverage=leverage,
            reduceOnly=reduceOnly,
            time_in_force=time_in_force,
        )
        if self.trading_mode == TradingMode.FUTURES and self.margin_mode:
            params['tdMode'] = self.margin_mode.value
            params['posSide'] = self._get_posSide(side, reduceOnly)
        return params

    @retrier
    def _lev_prep(self, pair: str, leverage: float, side: BuySell):
        if self.trading_mode != TradingMode.SPOT and self.margin_mode is not None:
            try:
                # TODO-lev: Test me properly (check mgnMode passed)
                self._api.set_leverage(
                    leverage=leverage,
                    symbol=pair,
                    params={
                        "mgnMode": self.margin_mode.value,
                        "posSide": self._get_posSide(side, False),
                    })
            except ccxt.DDoSProtection as e:
                raise DDosProtection(e) from e
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                raise TemporaryError(
                    f'Could not set leverage due to {e.__class__.__name__}. Message: {e}') from e
            except ccxt.BaseError as e:
                raise OperationalException(e) from e

    def get_max_pair_stake_amount(
        self,
        pair: str,
        price: float,
        leverage: float = 1.0
    ) -> float:

        if self.trading_mode == TradingMode.SPOT:
            return float('inf')  # Not actually inf, but this probably won't matter for SPOT

        if pair not in self._leverage_tiers:
            return float('inf')

        pair_tiers = self._leverage_tiers[pair]
        return pair_tiers[-1]['max'] / leverage
