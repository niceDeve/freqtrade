"""
PairList manager class
"""
import logging
from copy import deepcopy
from typing import Dict, List, Tuple

from cachetools import TTLCache, cached

from freqtrade.exceptions import OperationalException
from freqtrade.pairlist.IPairList import IPairList
from freqtrade.resolvers import PairListResolver


logger = logging.getLogger(__name__)


# List of pairs with their timeframes
ListPairsWithTimeframes = List[Tuple[str, str]]


class PairListManager():

    def __init__(self, exchange, config: dict) -> None:
        self._exchange = exchange
        self._config = config
        self._whitelist = self._config['exchange'].get('pair_whitelist')
        self._blacklist = self._config['exchange'].get('pair_blacklist', [])
        self._pairlists: List[IPairList] = []
        self._tickers_needed = False
        for pl in self._config.get('pairlists', None):
            if 'method' not in pl:
                logger.warning(f"No method in {pl}")
                continue
            pairl = PairListResolver.load_pairlist(pl.get('method'),
                                                   exchange=exchange,
                                                   pairlistmanager=self,
                                                   config=config,
                                                   pairlistconfig=pl,
                                                   pairlist_pos=len(self._pairlists)
                                                   )
            self._tickers_needed = pairl.needstickers or self._tickers_needed
            self._pairlists.append(pairl)

        if not self._pairlists:
            raise OperationalException("No Pairlist defined!")

    @property
    def whitelist(self) -> List[str]:
        """
        Has the current whitelist
        """
        return self._whitelist

    @property
    def blacklist(self) -> List[str]:
        """
        Has the current blacklist
        -> no need to overwrite in subclasses
        """
        return self._blacklist

    @property
    def name_list(self) -> List[str]:
        """
        Get list of loaded pairlists names
        """
        return [p.name for p in self._pairlists]

    def short_desc(self) -> List[Dict]:
        """
        List of short_desc for each pairlist
        """
        return [{p.name: p.short_desc()} for p in self._pairlists]

    @cached(TTLCache(maxsize=1, ttl=1800))
    def _get_cached_tickers(self):
        return self._exchange.get_tickers()

    def refresh_pairlist(self) -> None:
        """
        Run pairlist through all configured pairlists.
        """
        # Tickers should be cached to avoid calling the exchange on each call.
        tickers: Dict = {}
        if self._tickers_needed:
            tickers = self._get_cached_tickers()

        # Adjust whitelist if filters are using tickers
        pairlist = self._prepare_whitelist(self._whitelist.copy(), tickers)

        # Process all pairlists in chain
        for pl in self._pairlists:
            pairlist = pl.filter_pairlist(pairlist, tickers)

        # Validation against blacklist happens after the pairlists to ensure
        # blacklist is respected.
        pairlist = IPairList.verify_blacklist(pairlist, self.blacklist, True)

        self._whitelist = pairlist

    def _prepare_whitelist(self, pairlist: List[str], tickers) -> List[str]:
        """
        Prepare sanitized pairlist for Pairlist Filters that use tickers data - remove
        pairs that do not have ticker available
        """
        if self._tickers_needed:
            # Copy list since we're modifying this list
            for p in deepcopy(pairlist):
                if p not in tickers:
                    pairlist.remove(p)

        return pairlist

    def create_pair_list(self, pairs: List[str], timeframe: str = None) -> ListPairsWithTimeframes:
        """
        Create list of pair tuples with (pair, ticker_interval)
        """
        return [(pair, timeframe or self._config['ticker_interval']) for pair in pairs]