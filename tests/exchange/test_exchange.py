import copy
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isclose
from random import randint
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import arrow
import ccxt
import pytest
from pandas import DataFrame

from freqtrade.enums import CandleType, Collateral, TradingMode
from freqtrade.exceptions import (DDosProtection, DependencyException, InvalidOrderException,
                                  OperationalException, PricingError, TemporaryError)
from freqtrade.exchange import Binance, Bittrex, Exchange, Kraken
from freqtrade.exchange.common import (API_FETCH_ORDER_RETRY_COUNT, API_RETRY_COUNT,
                                       calculate_backoff, remove_credentials)
from freqtrade.exchange.exchange import (market_is_active, timeframe_to_minutes, timeframe_to_msecs,
                                         timeframe_to_next_date, timeframe_to_prev_date,
                                         timeframe_to_seconds)
from freqtrade.resolvers.exchange_resolver import ExchangeResolver
from tests.conftest import get_mock_coro, get_patched_exchange, log_has, log_has_re, num_log_has_re


# Make sure to always keep one exchange here which is NOT subclassed!!
EXCHANGES = ['bittrex', 'binance', 'kraken', 'ftx', 'gateio']
spot = TradingMode.SPOT
margin = TradingMode.MARGIN
futures = TradingMode.FUTURES

cross = Collateral.CROSS
isolated = Collateral.ISOLATED


def ccxt_exceptionhandlers(mocker, default_conf, api_mock, exchange_name,
                           fun, mock_ccxt_fun, retries=API_RETRY_COUNT + 1, **kwargs):

    with patch('freqtrade.exchange.common.time.sleep'):
        with pytest.raises(DDosProtection):
            api_mock.__dict__[mock_ccxt_fun] = MagicMock(side_effect=ccxt.DDoSProtection("DDos"))
            exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
            getattr(exchange, fun)(**kwargs)
        assert api_mock.__dict__[mock_ccxt_fun].call_count == retries

    with pytest.raises(TemporaryError):
        api_mock.__dict__[mock_ccxt_fun] = MagicMock(side_effect=ccxt.NetworkError("DeaDBeef"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        getattr(exchange, fun)(**kwargs)
    assert api_mock.__dict__[mock_ccxt_fun].call_count == retries

    with pytest.raises(OperationalException):
        api_mock.__dict__[mock_ccxt_fun] = MagicMock(side_effect=ccxt.BaseError("DeadBeef"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        getattr(exchange, fun)(**kwargs)
    assert api_mock.__dict__[mock_ccxt_fun].call_count == 1


async def async_ccxt_exception(mocker, default_conf, api_mock, fun, mock_ccxt_fun,
                               retries=API_RETRY_COUNT + 1, **kwargs):

    with patch('freqtrade.exchange.common.asyncio.sleep', get_mock_coro(None)):
        with pytest.raises(DDosProtection):
            api_mock.__dict__[mock_ccxt_fun] = MagicMock(side_effect=ccxt.DDoSProtection("Dooh"))
            exchange = get_patched_exchange(mocker, default_conf, api_mock)
            await getattr(exchange, fun)(**kwargs)
        assert api_mock.__dict__[mock_ccxt_fun].call_count == retries

    with pytest.raises(TemporaryError):
        api_mock.__dict__[mock_ccxt_fun] = MagicMock(side_effect=ccxt.NetworkError("DeadBeef"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock)
        await getattr(exchange, fun)(**kwargs)
    assert api_mock.__dict__[mock_ccxt_fun].call_count == retries

    with pytest.raises(OperationalException):
        api_mock.__dict__[mock_ccxt_fun] = MagicMock(side_effect=ccxt.BaseError("DeadBeef"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock)
        await getattr(exchange, fun)(**kwargs)
    assert api_mock.__dict__[mock_ccxt_fun].call_count == 1


def test_init(default_conf, mocker, caplog):
    caplog.set_level(logging.INFO)
    get_patched_exchange(mocker, default_conf)
    assert log_has('Instance is running with dry_run enabled', caplog)


def test_remove_credentials(default_conf, caplog) -> None:
    conf = deepcopy(default_conf)
    conf['dry_run'] = False
    remove_credentials(conf)

    assert conf['exchange']['key'] != ''
    assert conf['exchange']['secret'] != ''

    conf['dry_run'] = True
    remove_credentials(conf)
    assert conf['exchange']['key'] == ''
    assert conf['exchange']['secret'] == ''
    assert conf['exchange']['password'] == ''
    assert conf['exchange']['uid'] == ''


def test_init_ccxt_kwargs(default_conf, mocker, caplog):
    mocker.patch('freqtrade.exchange.Exchange._load_markets', MagicMock(return_value={}))
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')
    caplog.set_level(logging.INFO)
    conf = copy.deepcopy(default_conf)
    conf['exchange']['ccxt_async_config'] = {'aiohttp_trust_env': True, 'asyncio_loop': True}
    ex = Exchange(conf)
    assert log_has(
        "Applying additional ccxt config: {'aiohttp_trust_env': True, 'asyncio_loop': True}",
        caplog)
    assert ex._api_async.aiohttp_trust_env
    assert not ex._api.aiohttp_trust_env

    # Reset logging and config
    caplog.clear()
    conf = copy.deepcopy(default_conf)
    conf['exchange']['ccxt_config'] = {'TestKWARG': 11}
    conf['exchange']['ccxt_sync_config'] = {'TestKWARG44': 11}
    conf['exchange']['ccxt_async_config'] = {'asyncio_loop': True}
    asynclogmsg = "Applying additional ccxt config: {'TestKWARG': 11, 'asyncio_loop': True}"
    ex = Exchange(conf)
    assert not ex._api_async.aiohttp_trust_env
    assert hasattr(ex._api, 'TestKWARG')
    assert ex._api.TestKWARG == 11
    # ccxt_config is assigned to both sync and async
    assert not hasattr(ex._api_async, 'TestKWARG44')

    assert hasattr(ex._api_async, 'TestKWARG')
    assert log_has("Applying additional ccxt config: {'TestKWARG': 11, 'TestKWARG44': 11}", caplog)
    assert log_has(asynclogmsg, caplog)
    # Test additional headers case
    Exchange._headers = {'hello': 'world'}
    ex = Exchange(conf)

    assert log_has("Applying additional ccxt config: {'TestKWARG': 11, 'TestKWARG44': 11}", caplog)
    assert ex._api.headers == {'hello': 'world'}
    assert ex._ccxt_config == {}
    Exchange._headers = {}


def test_destroy(default_conf, mocker, caplog):
    caplog.set_level(logging.DEBUG)
    get_patched_exchange(mocker, default_conf)
    assert log_has('Exchange object destroyed, closing async loop', caplog)


def test_init_exception(default_conf, mocker):
    default_conf['exchange']['name'] = 'wrong_exchange_name'

    with pytest.raises(OperationalException,
                       match=f"Exchange {default_conf['exchange']['name']} is not supported"):
        Exchange(default_conf)

    default_conf['exchange']['name'] = 'binance'
    with pytest.raises(OperationalException,
                       match=f"Exchange {default_conf['exchange']['name']} is not supported"):
        mocker.patch("ccxt.binance", MagicMock(side_effect=AttributeError))
        Exchange(default_conf)

    with pytest.raises(OperationalException,
                       match=r"Initialization of ccxt failed. Reason: DeadBeef"):
        mocker.patch("ccxt.binance", MagicMock(side_effect=ccxt.BaseError("DeadBeef")))
        Exchange(default_conf)


def test_exchange_resolver(default_conf, mocker, caplog):
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=MagicMock()))
    mocker.patch('freqtrade.exchange.Exchange._load_async_markets')
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs')
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')

    exchange = ExchangeResolver.load_exchange('huobi', default_conf)
    assert isinstance(exchange, Exchange)
    assert log_has_re(r"No .* specific subclass found. Using the generic class instead.", caplog)
    caplog.clear()

    exchange = ExchangeResolver.load_exchange('Bittrex', default_conf)
    assert isinstance(exchange, Exchange)
    assert isinstance(exchange, Bittrex)
    assert not log_has_re(r"No .* specific subclass found. Using the generic class instead.",
                          caplog)
    caplog.clear()

    exchange = ExchangeResolver.load_exchange('kraken', default_conf)
    assert isinstance(exchange, Exchange)
    assert isinstance(exchange, Kraken)
    assert not isinstance(exchange, Binance)
    assert not log_has_re(r"No .* specific subclass found. Using the generic class instead.",
                          caplog)

    exchange = ExchangeResolver.load_exchange('binance', default_conf)
    assert isinstance(exchange, Exchange)
    assert isinstance(exchange, Binance)
    assert not isinstance(exchange, Kraken)

    assert not log_has_re(r"No .* specific subclass found. Using the generic class instead.",
                          caplog)

    # Test mapping
    exchange = ExchangeResolver.load_exchange('binanceus', default_conf)
    assert isinstance(exchange, Exchange)
    assert isinstance(exchange, Binance)
    assert not isinstance(exchange, Kraken)


def test_validate_order_time_in_force(default_conf, mocker, caplog):
    caplog.set_level(logging.INFO)
    # explicitly test bittrex, exchanges implementing other policies need separate tests
    ex = get_patched_exchange(mocker, default_conf, id="bittrex")
    tif = {
        "buy": "gtc",
        "sell": "gtc",
    }

    ex.validate_order_time_in_force(tif)
    tif2 = {
        "buy": "fok",
        "sell": "ioc",
    }
    with pytest.raises(OperationalException, match=r"Time in force.*not supported for .*"):
        ex.validate_order_time_in_force(tif2)

    # Patch to see if this will pass if the values are in the ft dict
    ex._ft_has.update({"order_time_in_force": ["gtc", "fok", "ioc"]})
    ex.validate_order_time_in_force(tif2)


@pytest.mark.parametrize("amount,precision_mode,precision,contract_size,expected,trading_mode", [
    (2.34559, 2, 4, 1, 2.3455, 'spot'),
    (2.34559, 2, 5, 1, 2.34559, 'spot'),
    (2.34559, 2, 3, 1, 2.345, 'spot'),
    (2.9999, 2, 3, 1, 2.999, 'spot'),
    (2.9909, 2, 3, 1, 2.990, 'spot'),
    # Tests for Tick-size
    (2.34559, 4, 0.0001, 1, 2.3455, 'spot'),
    (2.34559, 4, 0.00001, 1, 2.34559, 'spot'),
    (2.34559, 4, 0.001, 1, 2.345, 'spot'),
    (2.9999, 4, 0.001, 1, 2.999, 'spot'),
    (2.9909, 4, 0.001, 1, 2.990, 'spot'),
    (2.9909, 4, 0.005, 0.01, 299.09, 'futures'),
    (2.9999, 4, 0.005, 10, 0.295, 'futures'),
])
def test_amount_to_precision(
    default_conf,
    mocker,
    amount,
    precision_mode,
    precision,
    contract_size,
    expected,
    trading_mode
):
    """
    Test rounds down
    """

    markets = PropertyMock(return_value={
        'ETH/BTC': {
            'contractSize': contract_size,
            'precision': {
                'amount': precision
            }
        }
    })

    default_conf['trading_mode'] = trading_mode
    default_conf['collateral'] = 'isolated'

    exchange = get_patched_exchange(mocker, default_conf, id="binance")
    # digits counting mode
    # DECIMAL_PLACES = 2
    # SIGNIFICANT_DIGITS = 3
    # TICK_SIZE = 4
    mocker.patch('freqtrade.exchange.Exchange.precisionMode',
                 PropertyMock(return_value=precision_mode))
    mocker.patch('freqtrade.exchange.Exchange.markets', markets)

    pair = 'ETH/BTC'
    assert exchange.amount_to_precision(pair, amount) == expected


@pytest.mark.parametrize("price,precision_mode,precision,expected", [
    (2.34559, 2, 4, 2.3456),
    (2.34559, 2, 5, 2.34559),
    (2.34559, 2, 3, 2.346),
    (2.9999, 2, 3, 3.000),
    (2.9909, 2, 3, 2.991),
    # Tests for Tick_size
    (2.34559, 4, 0.0001, 2.3456),
    (2.34559, 4, 0.00001, 2.34559),
    (2.34559, 4, 0.001, 2.346),
    (2.9999, 4, 0.001, 3.000),
    (2.9909, 4, 0.001, 2.991),
    (2.9909, 4, 0.005, 2.995),
    (2.9973, 4, 0.005, 3.0),
    (2.9977, 4, 0.005, 3.0),
    (234.43, 4, 0.5, 234.5),
    (234.53, 4, 0.5, 235.0),
    (0.891534, 4, 0.0001, 0.8916),
    (64968.89, 4, 0.01, 64968.89),

])
def test_price_to_precision(default_conf, mocker, price, precision_mode, precision, expected):
    """Test price to precision"""
    markets = PropertyMock(return_value={'ETH/BTC': {'precision': {'price': precision}}})

    exchange = get_patched_exchange(mocker, default_conf, id="binance")
    mocker.patch('freqtrade.exchange.Exchange.markets', markets)
    # digits counting mode
    # DECIMAL_PLACES = 2
    # SIGNIFICANT_DIGITS = 3
    # TICK_SIZE = 4
    mocker.patch('freqtrade.exchange.Exchange.precisionMode',
                 PropertyMock(return_value=precision_mode))

    pair = 'ETH/BTC'
    assert exchange.price_to_precision(pair, price) == expected


@pytest.mark.parametrize("price,precision_mode,precision,expected", [
    (2.34559, 2, 4, 0.0001),
    (2.34559, 2, 5, 0.00001),
    (2.34559, 2, 3, 0.001),
    (2.9999, 2, 3, 0.001),
    (200.0511, 2, 3, 0.001),
    # Tests for Tick_size
    (2.34559, 4, 0.0001, 0.0001),
    (2.34559, 4, 0.00001, 0.00001),
    (2.34559, 4, 0.0025, 0.0025),
    (2.9909, 4, 0.0025, 0.0025),
    (234.43, 4, 0.5, 0.5),
    (234.43, 4, 0.0025, 0.0025),
    (234.43, 4, 0.00013, 0.00013),

])
def test_price_get_one_pip(default_conf, mocker, price, precision_mode, precision, expected):
    markets = PropertyMock(return_value={'ETH/BTC': {'precision': {'price': precision}}})
    exchange = get_patched_exchange(mocker, default_conf, id="binance")
    mocker.patch('freqtrade.exchange.Exchange.markets', markets)
    mocker.patch('freqtrade.exchange.Exchange.precisionMode',
                 PropertyMock(return_value=precision_mode))
    pair = 'ETH/BTC'
    assert pytest.approx(exchange.price_get_one_pip(pair, price)) == expected


def test_get_min_pair_stake_amount(mocker, default_conf) -> None:

    exchange = get_patched_exchange(mocker, default_conf, id="binance")
    stoploss = -0.05
    markets = {'ETH/BTC': {'symbol': 'ETH/BTC'}}

    # no pair found
    mocker.patch(
        'freqtrade.exchange.Exchange.markets',
        PropertyMock(return_value=markets)
    )
    with pytest.raises(ValueError, match=r'.*get market information.*'):
        exchange.get_min_pair_stake_amount('BNB/BTC', 1, stoploss)

    # no cost Min
    markets["ETH/BTC"]["limits"] = {
        'cost': {'min': None, 'max': None},
        'amount': {'min': None, 'max': None},
    }
    mocker.patch(
        'freqtrade.exchange.Exchange.markets',
        PropertyMock(return_value=markets)
    )
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 1, stoploss)
    assert result is None

    # no amount Min
    markets["ETH/BTC"]["limits"] = {
        'cost': {'min': None, 'max': None},
        'amount': {'min': None, 'max': None},
    }
    mocker.patch(
        'freqtrade.exchange.Exchange.markets',
        PropertyMock(return_value=markets)
    )
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 1, stoploss)
    assert result is None

    # empty 'cost'/'amount' section
    markets["ETH/BTC"]["limits"] = {
        'cost': {'min': None, 'max': None},
        'amount': {'min': None, 'max': None},
    }
    mocker.patch(
        'freqtrade.exchange.Exchange.markets',
        PropertyMock(return_value=markets)
    )
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 1, stoploss)
    assert result is None

    # min cost is set
    markets["ETH/BTC"]["limits"] = {
        'cost': {'min': 2, 'max': None},
        'amount': {'min': None, 'max': None},
    }
    mocker.patch(
        'freqtrade.exchange.Exchange.markets',
        PropertyMock(return_value=markets)
    )
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 1, stoploss)
    expected_result = 2 * (1+0.05) / (1-abs(stoploss))
    assert isclose(result, expected_result)
    # With Leverage
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 1, stoploss, 3.0)
    assert isclose(result, expected_result/3)

    # min amount is set
    markets["ETH/BTC"]["limits"] = {
        'cost': {'min': None, 'max': None},
        'amount': {'min': 2, 'max': None},
    }
    mocker.patch(
        'freqtrade.exchange.Exchange.markets',
        PropertyMock(return_value=markets)
    )
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 2, stoploss)
    expected_result = 2 * 2 * (1+0.05) / (1-abs(stoploss))
    assert isclose(result, expected_result)
    # With Leverage
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 2, stoploss, 5.0)
    assert isclose(result, expected_result/5)

    # min amount and cost are set (cost is minimal)
    markets["ETH/BTC"]["limits"] = {
        'cost': {'min': 2, 'max': None},
        'amount': {'min': 2, 'max': None},
    }
    mocker.patch(
        'freqtrade.exchange.Exchange.markets',
        PropertyMock(return_value=markets)
    )
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 2, stoploss)
    expected_result = max(2, 2 * 2) * (1+0.05) / (1-abs(stoploss))
    assert isclose(result, expected_result)
    # With Leverage
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 2, stoploss, 10)
    assert isclose(result, expected_result/10)

    # min amount and cost are set (amount is minial)
    markets["ETH/BTC"]["limits"] = {
        'cost': {'min': 8, 'max': None},
        'amount': {'min': 2, 'max': None},
    }
    mocker.patch(
        'freqtrade.exchange.Exchange.markets',
        PropertyMock(return_value=markets)
    )
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 2, stoploss)
    expected_result = max(8, 2 * 2) * (1+0.05) / (1-abs(stoploss))
    assert isclose(result, expected_result)
    # With Leverage
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 2, stoploss, 7.0)
    assert isclose(result, expected_result/7.0)

    result = exchange.get_min_pair_stake_amount('ETH/BTC', 2, -0.4)
    expected_result = max(8, 2 * 2) * 1.5
    assert isclose(result, expected_result)
    # With Leverage
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 2, -0.4, 8.0)
    assert isclose(result, expected_result/8.0)

    # Really big stoploss
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 2, -1)
    expected_result = max(8, 2 * 2) * 1.5
    assert isclose(result, expected_result)
    # With Leverage
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 2, -1, 12.0)
    assert isclose(result, expected_result/12)

    markets["ETH/BTC"]["contractSize"] = '0.01'
    default_conf['trading_mode'] = 'futures'
    default_conf['collateral'] = 'isolated'
    exchange = get_patched_exchange(mocker, default_conf, id="binance")
    mocker.patch(
        'freqtrade.exchange.Exchange.markets',
        PropertyMock(return_value=markets)
    )

    # Contract size 0.01
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 2, -1)
    assert isclose(result, expected_result * 0.01)

    markets["ETH/BTC"]["contractSize"] = '10'
    mocker.patch(
        'freqtrade.exchange.Exchange.markets',
        PropertyMock(return_value=markets)
    )
    # With Leverage, Contract size 10
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 2, -1, 12.0)
    assert isclose(result, (expected_result/12) * 10.0)


def test_get_min_pair_stake_amount_real_data(mocker, default_conf) -> None:
    exchange = get_patched_exchange(mocker, default_conf, id="binance")
    stoploss = -0.05
    markets = {'ETH/BTC': {'symbol': 'ETH/BTC'}}

    # Real Binance data
    markets["ETH/BTC"]["limits"] = {
        'cost': {'min': 0.0001},
        'amount': {'min': 0.001}
    }
    mocker.patch(
        'freqtrade.exchange.Exchange.markets',
        PropertyMock(return_value=markets)
    )
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 0.020405, stoploss)
    expected_result = max(0.0001, 0.001 * 0.020405) * (1+0.05) / (1-abs(stoploss))
    assert round(result, 8) == round(expected_result, 8)
    result = exchange.get_min_pair_stake_amount('ETH/BTC', 0.020405, stoploss, 3.0)
    assert round(result, 8) == round(expected_result/3, 8)


def test_set_sandbox(default_conf, mocker):
    """
    Test working scenario
    """
    api_mock = MagicMock()
    api_mock.load_markets = MagicMock(return_value={
        'ETH/BTC': '', 'LTC/BTC': '', 'XRP/BTC': '', 'NEO/BTC': ''
    })
    url_mock = PropertyMock(return_value={'test': "api-public.sandbox.gdax.com",
                                          'api': 'https://api.gdax.com'})
    type(api_mock).urls = url_mock
    exchange = get_patched_exchange(mocker, default_conf, api_mock)
    liveurl = exchange._api.urls['api']
    default_conf['exchange']['sandbox'] = True
    exchange.set_sandbox(exchange._api, default_conf['exchange'], 'Logname')
    assert exchange._api.urls['api'] != liveurl


def test_set_sandbox_exception(default_conf, mocker):
    """
    Test Fail scenario
    """
    api_mock = MagicMock()
    api_mock.load_markets = MagicMock(return_value={
        'ETH/BTC': '', 'LTC/BTC': '', 'XRP/BTC': '', 'NEO/BTC': ''
    })
    url_mock = PropertyMock(return_value={'api': 'https://api.gdax.com'})
    type(api_mock).urls = url_mock

    with pytest.raises(OperationalException, match=r'does not provide a sandbox api'):
        exchange = get_patched_exchange(mocker, default_conf, api_mock)
        default_conf['exchange']['sandbox'] = True
        exchange.set_sandbox(exchange._api, default_conf['exchange'], 'Logname')


def test__load_async_markets(default_conf, mocker, caplog):
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt')
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs')
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange._load_markets')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')
    exchange = Exchange(default_conf)
    exchange._api_async.load_markets = get_mock_coro(None)
    exchange._load_async_markets()
    assert exchange._api_async.load_markets.call_count == 1
    caplog.set_level(logging.DEBUG)

    exchange._api_async.load_markets = Mock(side_effect=ccxt.BaseError("deadbeef"))
    exchange._load_async_markets()

    assert log_has('Could not load async markets. Reason: deadbeef', caplog)


def test__load_markets(default_conf, mocker, caplog):
    caplog.set_level(logging.INFO)
    api_mock = MagicMock()
    api_mock.load_markets = MagicMock(side_effect=ccxt.BaseError("SomeError"))
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs')
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange._load_async_markets')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')
    Exchange(default_conf)
    assert log_has('Unable to initialize markets.', caplog)

    expected_return = {'ETH/BTC': 'available'}
    api_mock = MagicMock()
    api_mock.load_markets = MagicMock(return_value=expected_return)
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    default_conf['exchange']['pair_whitelist'] = ['ETH/BTC']
    ex = Exchange(default_conf)

    assert ex.markets == expected_return


def test_reload_markets(default_conf, mocker, caplog):
    caplog.set_level(logging.DEBUG)
    initial_markets = {'ETH/BTC': {}}
    updated_markets = {'ETH/BTC': {}, "LTC/BTC": {}}

    api_mock = MagicMock()
    api_mock.load_markets = MagicMock(return_value=initial_markets)
    default_conf['exchange']['markets_refresh_interval'] = 10
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id="binance",
                                    mock_markets=False)
    exchange._load_async_markets = MagicMock()
    exchange._last_markets_refresh = arrow.utcnow().int_timestamp

    assert exchange.markets == initial_markets

    # less than 10 minutes have passed, no reload
    exchange.reload_markets()
    assert exchange.markets == initial_markets
    assert exchange._load_async_markets.call_count == 0

    api_mock.load_markets = MagicMock(return_value=updated_markets)
    # more than 10 minutes have passed, reload is executed
    exchange._last_markets_refresh = arrow.utcnow().int_timestamp - 15 * 60
    exchange.reload_markets()
    assert exchange.markets == updated_markets
    assert exchange._load_async_markets.call_count == 1
    assert log_has('Performing scheduled market reload..', caplog)


def test_reload_markets_exception(default_conf, mocker, caplog):
    caplog.set_level(logging.DEBUG)

    api_mock = MagicMock()
    api_mock.load_markets = MagicMock(side_effect=ccxt.NetworkError("LoadError"))
    default_conf['exchange']['markets_refresh_interval'] = 10
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id="binance")

    # less than 10 minutes have passed, no reload
    exchange.reload_markets()
    assert exchange._last_markets_refresh == 0
    assert log_has_re(r"Could not reload markets.*", caplog)


@pytest.mark.parametrize("stake_currency", ['ETH', 'BTC', 'USDT'])
def test_validate_stakecurrency(default_conf, stake_currency, mocker, caplog):
    default_conf['stake_currency'] = stake_currency
    api_mock = MagicMock()
    type(api_mock).load_markets = MagicMock(return_value={
        'ETH/BTC': {'quote': 'BTC'}, 'LTC/BTC': {'quote': 'BTC'},
        'XRP/ETH': {'quote': 'ETH'}, 'NEO/USDT': {'quote': 'USDT'},
    })
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs')
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange._load_async_markets')
    Exchange(default_conf)


def test_validate_stakecurrency_error(default_conf, mocker, caplog):
    default_conf['stake_currency'] = 'XRP'
    api_mock = MagicMock()
    type(api_mock).load_markets = MagicMock(return_value={
        'ETH/BTC': {'quote': 'BTC'}, 'LTC/BTC': {'quote': 'BTC'},
        'XRP/ETH': {'quote': 'ETH'}, 'NEO/USDT': {'quote': 'USDT'},
    })
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs')
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange._load_async_markets')
    with pytest.raises(OperationalException,
                       match=r'XRP is not available as stake on .*'
                       'Available currencies are: BTC, ETH, USDT'):
        Exchange(default_conf)

    type(api_mock).load_markets = MagicMock(side_effect=ccxt.NetworkError('No connection.'))
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))

    with pytest.raises(OperationalException,
                       match=r'Could not load markets, therefore cannot start\. Please.*'):
        Exchange(default_conf)


def test_get_quote_currencies(default_conf, mocker):
    ex = get_patched_exchange(mocker, default_conf)

    assert set(ex.get_quote_currencies()) == set(['USD', 'ETH', 'BTC', 'USDT'])


@pytest.mark.parametrize('pair,expected', [
    ('XRP/BTC', 'BTC'),
    ('LTC/USD', 'USD'),
    ('ETH/USDT', 'USDT'),
    ('XLTCUSDT', 'USDT'),
    ('XRP/NOCURRENCY', ''),
])
def test_get_pair_quote_currency(default_conf, mocker, pair, expected):
    ex = get_patched_exchange(mocker, default_conf)
    assert ex.get_pair_quote_currency(pair) == expected


@pytest.mark.parametrize('pair,expected', [
    ('XRP/BTC', 'XRP'),
    ('LTC/USD', 'LTC'),
    ('ETH/USDT', 'ETH'),
    ('XLTCUSDT', 'LTC'),
    ('XRP/NOCURRENCY', ''),
])
def test_get_pair_base_currency(default_conf, mocker, pair, expected):
    ex = get_patched_exchange(mocker, default_conf)
    assert ex.get_pair_base_currency(pair) == expected


def test_validate_pairs(default_conf, mocker):  # test exchange.validate_pairs directly
    api_mock = MagicMock()
    type(api_mock).load_markets = MagicMock(return_value={
        'ETH/BTC': {'quote': 'BTC'},
        'LTC/BTC': {'quote': 'BTC'},
        'XRP/BTC': {'quote': 'BTC'},
        'NEO/BTC': {'quote': 'BTC'},
    })
    id_mock = PropertyMock(return_value='test_exchange')
    type(api_mock).id = id_mock

    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange._load_async_markets')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')
    Exchange(default_conf)


def test_validate_pairs_not_available(default_conf, mocker):
    api_mock = MagicMock()
    type(api_mock).markets = PropertyMock(return_value={
        'XRP/BTC': {'inactive': True, 'base': 'XRP', 'quote': 'BTC'}
    })
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')
    mocker.patch('freqtrade.exchange.Exchange._load_async_markets')

    with pytest.raises(OperationalException, match=r'not available'):
        Exchange(default_conf)


def test_validate_pairs_exception(default_conf, mocker, caplog):
    caplog.set_level(logging.INFO)
    api_mock = MagicMock()
    mocker.patch('freqtrade.exchange.Exchange.name', PropertyMock(return_value='Binance'))

    type(api_mock).markets = PropertyMock(return_value={})
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', api_mock)
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')
    mocker.patch('freqtrade.exchange.Exchange._load_async_markets')

    with pytest.raises(OperationalException, match=r'Pair ETH/BTC is not available on Binance'):
        Exchange(default_conf)

    mocker.patch('freqtrade.exchange.Exchange.markets', PropertyMock(return_value={}))
    Exchange(default_conf)
    assert log_has('Unable to validate pairs (assuming they are correct).', caplog)


def test_validate_pairs_restricted(default_conf, mocker, caplog):
    api_mock = MagicMock()
    type(api_mock).load_markets = MagicMock(return_value={
        'ETH/BTC': {'quote': 'BTC'}, 'LTC/BTC': {'quote': 'BTC'},
        'XRP/BTC': {'quote': 'BTC', 'info': {'prohibitedIn': ['US']}},
        'NEO/BTC': {'quote': 'BTC', 'info': 'TestString'},  # info can also be a string ...
    })
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange._load_async_markets')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')

    Exchange(default_conf)
    assert log_has("Pair XRP/BTC is restricted for some users on this exchange."
                   "Please check if you are impacted by this restriction "
                   "on the exchange and eventually remove XRP/BTC from your whitelist.", caplog)


def test_validate_pairs_stakecompatibility(default_conf, mocker, caplog):
    api_mock = MagicMock()
    type(api_mock).load_markets = MagicMock(return_value={
        'ETH/BTC': {'quote': 'BTC'}, 'LTC/BTC': {'quote': 'BTC'},
        'XRP/BTC': {'quote': 'BTC'}, 'NEO/BTC': {'quote': 'BTC'},
        'HELLO-WORLD': {'quote': 'BTC'},
    })
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange._load_async_markets')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')

    Exchange(default_conf)


def test_validate_pairs_stakecompatibility_downloaddata(default_conf, mocker, caplog):
    api_mock = MagicMock()
    default_conf['stake_currency'] = ''
    type(api_mock).load_markets = MagicMock(return_value={
        'ETH/BTC': {'quote': 'BTC'}, 'LTC/BTC': {'quote': 'BTC'},
        'XRP/BTC': {'quote': 'BTC'}, 'NEO/BTC': {'quote': 'BTC'},
        'HELLO-WORLD': {'quote': 'BTC'},
    })
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange._load_async_markets')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')

    Exchange(default_conf)
    assert type(api_mock).load_markets.call_count == 1


def test_validate_pairs_stakecompatibility_fail(default_conf, mocker, caplog):
    default_conf['exchange']['pair_whitelist'].append('HELLO-WORLD')
    api_mock = MagicMock()
    type(api_mock).load_markets = MagicMock(return_value={
        'ETH/BTC': {'quote': 'BTC'}, 'LTC/BTC': {'quote': 'BTC'},
        'XRP/BTC': {'quote': 'BTC'}, 'NEO/BTC': {'quote': 'BTC'},
        'HELLO-WORLD': {'quote': 'USDT'},
    })
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange._load_async_markets')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')

    with pytest.raises(OperationalException, match=r"Stake-currency 'BTC' not compatible with.*"):
        Exchange(default_conf)


@pytest.mark.parametrize("timeframe", [
    ('5m'), ("1m"), ("15m"), ("1h")
])
def test_validate_timeframes(default_conf, mocker, timeframe):
    default_conf["timeframe"] = timeframe
    api_mock = MagicMock()
    id_mock = PropertyMock(return_value='test_exchange')
    type(api_mock).id = id_mock
    timeframes = PropertyMock(return_value={'1m': '1m',
                                            '5m': '5m',
                                            '15m': '15m',
                                            '1h': '1h'})
    type(api_mock).timeframes = timeframes

    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange._load_markets', MagicMock(return_value={}))
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')
    Exchange(default_conf)


def test_validate_timeframes_failed(default_conf, mocker):
    default_conf["timeframe"] = "3m"
    api_mock = MagicMock()
    id_mock = PropertyMock(return_value='test_exchange')
    type(api_mock).id = id_mock
    timeframes = PropertyMock(return_value={'15s': '15s',
                                            '1m': '1m',
                                            '5m': '5m',
                                            '15m': '15m',
                                            '1h': '1h'})
    type(api_mock).timeframes = timeframes

    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange._load_markets', MagicMock(return_value={}))
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs', MagicMock())
    with pytest.raises(OperationalException,
                       match=r"Invalid timeframe '3m'. This exchange supports.*"):
        Exchange(default_conf)
    default_conf["timeframe"] = "15s"

    with pytest.raises(OperationalException,
                       match=r"Timeframes < 1m are currently not supported by Freqtrade."):
        Exchange(default_conf)


def test_validate_timeframes_emulated_ohlcv_1(default_conf, mocker):
    default_conf["timeframe"] = "3m"
    api_mock = MagicMock()
    id_mock = PropertyMock(return_value='test_exchange')
    type(api_mock).id = id_mock

    # delete timeframes so magicmock does not autocreate it
    del api_mock.timeframes

    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange._load_markets', MagicMock(return_value={}))
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')
    with pytest.raises(OperationalException,
                       match=r'The ccxt library does not provide the list of timeframes '
                             r'for the exchange ".*" and this exchange '
                             r'is therefore not supported. *'):
        Exchange(default_conf)


def test_validate_timeframes_emulated_ohlcvi_2(default_conf, mocker):
    default_conf["timeframe"] = "3m"
    api_mock = MagicMock()
    id_mock = PropertyMock(return_value='test_exchange')
    type(api_mock).id = id_mock

    # delete timeframes so magicmock does not autocreate it
    del api_mock.timeframes

    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange._load_markets',
                 MagicMock(return_value={'timeframes': None}))
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs', MagicMock())
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')
    with pytest.raises(OperationalException,
                       match=r'The ccxt library does not provide the list of timeframes '
                             r'for the exchange ".*" and this exchange '
                             r'is therefore not supported. *'):
        Exchange(default_conf)


def test_validate_timeframes_not_in_config(default_conf, mocker):
    del default_conf["timeframe"]
    api_mock = MagicMock()
    id_mock = PropertyMock(return_value='test_exchange')
    type(api_mock).id = id_mock
    timeframes = PropertyMock(return_value={'1m': '1m',
                                            '5m': '5m',
                                            '15m': '15m',
                                            '1h': '1h'})
    type(api_mock).timeframes = timeframes

    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange._load_markets', MagicMock(return_value={}))
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')
    Exchange(default_conf)


def test_validate_order_types(default_conf, mocker):
    api_mock = MagicMock()

    type(api_mock).has = PropertyMock(return_value={'createMarketOrder': True})
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange._load_markets', MagicMock(return_value={}))
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs')
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')
    mocker.patch('freqtrade.exchange.Exchange.name', 'Bittrex')

    default_conf['order_types'] = {
        'buy': 'limit',
        'sell': 'limit',
        'stoploss': 'market',
        'stoploss_on_exchange': False
    }
    Exchange(default_conf)

    type(api_mock).has = PropertyMock(return_value={'createMarketOrder': False})
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))

    default_conf['order_types'] = {
        'buy': 'limit',
        'sell': 'limit',
        'stoploss': 'market',
        'stoploss_on_exchange': False
    }
    with pytest.raises(OperationalException,
                       match=r'Exchange .* does not support market orders.'):
        Exchange(default_conf)

    default_conf['order_types'] = {
        'buy': 'limit',
        'sell': 'limit',
        'stoploss': 'limit',
        'stoploss_on_exchange': True
    }
    with pytest.raises(OperationalException,
                       match=r'On exchange stoploss is not supported for .*'):
        Exchange(default_conf)


def test_validate_order_types_not_in_config(default_conf, mocker):
    api_mock = MagicMock()
    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', MagicMock(return_value=api_mock))
    mocker.patch('freqtrade.exchange.Exchange._load_markets', MagicMock(return_value={}))
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs')
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')

    conf = copy.deepcopy(default_conf)
    Exchange(conf)


def test_validate_required_startup_candles(default_conf, mocker, caplog):
    api_mock = MagicMock()
    mocker.patch('freqtrade.exchange.Exchange.name', PropertyMock(return_value='Binance'))

    mocker.patch('freqtrade.exchange.Exchange._init_ccxt', api_mock)
    mocker.patch('freqtrade.exchange.Exchange.validate_timeframes')
    mocker.patch('freqtrade.exchange.Exchange._load_async_markets')
    mocker.patch('freqtrade.exchange.Exchange.validate_pairs')
    mocker.patch('freqtrade.exchange.Exchange.validate_stakecurrency')

    default_conf['startup_candle_count'] = 20
    ex = Exchange(default_conf)
    assert ex
    # assumption is that the exchange provides 500 candles per call.s
    assert ex.validate_required_startup_candles(200, '5m') == 1
    assert ex.validate_required_startup_candles(499, '5m') == 1
    assert ex.validate_required_startup_candles(600, '5m') == 2
    assert ex.validate_required_startup_candles(501, '5m') == 2
    assert ex.validate_required_startup_candles(499, '5m') == 1
    assert ex.validate_required_startup_candles(1000, '5m') == 3
    assert ex.validate_required_startup_candles(2499, '5m') == 5
    assert log_has_re(r'Using 5 calls to get OHLCV. This.*', caplog)

    with pytest.raises(OperationalException, match=r'This strategy requires 2500.*'):
        ex.validate_required_startup_candles(2500, '5m')

    # Ensure the same also happens on init
    default_conf['startup_candle_count'] = 6000
    with pytest.raises(OperationalException, match=r'This strategy requires 6000.*'):
        Exchange(default_conf)


def test_exchange_has(default_conf, mocker):
    exchange = get_patched_exchange(mocker, default_conf)
    assert not exchange.exchange_has('ASDFASDF')
    api_mock = MagicMock()

    type(api_mock).has = PropertyMock(return_value={'deadbeef': True})
    exchange = get_patched_exchange(mocker, default_conf, api_mock)
    assert exchange.exchange_has("deadbeef")

    type(api_mock).has = PropertyMock(return_value={'deadbeef': False})
    exchange = get_patched_exchange(mocker, default_conf, api_mock)
    assert not exchange.exchange_has("deadbeef")


@pytest.mark.parametrize("side", [
    ("buy"),
    ("sell")
])
@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_create_dry_run_order(default_conf, mocker, side, exchange_name):
    default_conf['dry_run'] = True
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)

    order = exchange.create_dry_run_order(
        pair='ETH/BTC',
        ordertype='limit',
        side=side,
        amount=1,
        rate=200,
        leverage=1.0
    )
    assert 'id' in order
    assert f'dry_run_{side}_' in order["id"]
    assert order["side"] == side
    assert order["type"] == "limit"
    assert order["symbol"] == "ETH/BTC"
    assert order["amount"] == 1


@pytest.mark.parametrize("side,startprice,endprice", [
    ("buy", 25.563, 25.566),
    ("sell", 25.566, 25.563)
])
@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_create_dry_run_order_limit_fill(default_conf, mocker, side, startprice, endprice,
                                         exchange_name, order_book_l2_usd):
    default_conf['dry_run'] = True
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    mocker.patch.multiple('freqtrade.exchange.Exchange',
                          exchange_has=MagicMock(return_value=True),
                          fetch_l2_order_book=order_book_l2_usd,
                          )

    order = exchange.create_dry_run_order(
        pair='LTC/USDT',
        ordertype='limit',
        side=side,
        amount=1,
        rate=startprice,
        leverage=1.0
    )
    assert order_book_l2_usd.call_count == 1
    assert 'id' in order
    assert f'dry_run_{side}_' in order["id"]
    assert order["side"] == side
    assert order["type"] == "limit"
    assert order["symbol"] == "LTC/USDT"
    order_book_l2_usd.reset_mock()

    order_closed = exchange.fetch_dry_run_order(order['id'])
    assert order_book_l2_usd.call_count == 1
    assert order_closed['status'] == 'open'
    assert not order['fee']
    assert order_closed['filled'] == 0

    order_book_l2_usd.reset_mock()
    order_closed['price'] = endprice

    order_closed = exchange.fetch_dry_run_order(order['id'])
    assert order_closed['status'] == 'closed'
    assert order['fee']
    assert order_closed['filled'] == 1
    assert order_closed['filled'] == order_closed['amount']

    # Empty orderbook test
    mocker.patch('freqtrade.exchange.Exchange.fetch_l2_order_book',
                 return_value={'asks': [], 'bids': []})
    exchange._dry_run_open_orders[order['id']]['status'] = 'open'
    order_closed = exchange.fetch_dry_run_order(order['id'])


@pytest.mark.parametrize("side,rate,amount,endprice", [
    # spread is 25.263-25.266
    ("buy", 25.564, 1, 25.566),
    ("buy", 25.564, 100, 25.5672),  # Requires interpolation
    ("buy", 25.590, 100, 25.5672),  # Price above spread ... average is lower
    ("buy", 25.564, 1000, 25.575),  # More than orderbook return
    ("buy", 24.000, 100000, 25.200),  # Run into max_slippage of 5%
    ("sell", 25.564, 1, 25.563),
    ("sell", 25.564, 100, 25.5625),  # Requires interpolation
    ("sell", 25.510, 100, 25.5625),  # price below spread - average is higher
    ("sell", 25.564, 1000, 25.5555),  # More than orderbook return
    ("sell", 27, 10000, 25.65),  # max-slippage 5%
])
@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_create_dry_run_order_market_fill(default_conf, mocker, side, rate, amount, endprice,
                                          exchange_name, order_book_l2_usd):
    default_conf['dry_run'] = True
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    mocker.patch.multiple('freqtrade.exchange.Exchange',
                          exchange_has=MagicMock(return_value=True),
                          fetch_l2_order_book=order_book_l2_usd,
                          )

    order = exchange.create_dry_run_order(
        pair='LTC/USDT',
        ordertype='market',
        side=side,
        amount=amount,
        rate=rate,
        leverage=1.0
    )
    assert 'id' in order
    assert f'dry_run_{side}_' in order["id"]
    assert order["side"] == side
    assert order["type"] == "market"
    assert order["symbol"] == "LTC/USDT"
    assert order['status'] == 'closed'
    assert order['filled'] == amount
    assert round(order["average"], 4) == round(endprice, 4)


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("ordertype,rate,marketprice", [
    ("market", None, None),
    ("market", 200, True),
    ("limit", 200, None),
    ("stop_loss_limit", 200, None)
])
@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_create_order(default_conf, mocker, side, ordertype, rate, marketprice, exchange_name):
    api_mock = MagicMock()
    order_id = 'test_prod_{}_{}'.format(side, randint(0, 10 ** 6))
    api_mock.options = {} if not marketprice else {"createMarketBuyOrderRequiresPrice": True}
    api_mock.create_order = MagicMock(return_value={
        'id': order_id,
        'info': {
            'foo': 'bar'
        },
        'symbol': 'XLTCUSDT',
        'amount': 1
    })
    default_conf['dry_run'] = False
    default_conf['collateral'] = 'isolated'
    mocker.patch('freqtrade.exchange.Exchange.amount_to_precision', lambda s, x, y: y)
    mocker.patch('freqtrade.exchange.Exchange.price_to_precision', lambda s, x, y: y)
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    exchange._set_leverage = MagicMock()
    exchange.set_margin_mode = MagicMock()

    order = exchange.create_order(
        pair='XLTCUSDT',
        ordertype=ordertype,
        side=side,
        amount=1,
        rate=200,
        leverage=1.0
    )

    assert 'id' in order
    assert 'info' in order
    assert order['id'] == order_id
    assert order['amount'] == 1
    assert api_mock.create_order.call_args[0][0] == 'XLTCUSDT'
    assert api_mock.create_order.call_args[0][1] == ordertype
    assert api_mock.create_order.call_args[0][2] == side
    assert api_mock.create_order.call_args[0][3] == 1
    assert api_mock.create_order.call_args[0][4] is rate
    assert exchange._set_leverage.call_count == 0
    assert exchange.set_margin_mode.call_count == 0

    exchange.trading_mode = TradingMode.FUTURES
    order = exchange.create_order(
        pair='XLTCUSDT',
        ordertype=ordertype,
        side=side,
        amount=1,
        rate=200,
        leverage=3.0
    )

    assert exchange._set_leverage.call_count == 1
    assert exchange.set_margin_mode.call_count == 1
    assert order['amount'] == 0.01


def test_buy_dry_run(default_conf, mocker):
    default_conf['dry_run'] = True
    exchange = get_patched_exchange(mocker, default_conf)

    order = exchange.create_order(pair='ETH/BTC', ordertype='limit', side="buy",
                                  amount=1, rate=200, time_in_force='gtc')
    assert 'id' in order
    assert 'dry_run_buy_' in order['id']


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_buy_prod(default_conf, mocker, exchange_name):
    api_mock = MagicMock()
    order_id = 'test_prod_buy_{}'.format(randint(0, 10 ** 6))
    order_type = 'market'
    time_in_force = 'gtc'
    api_mock.options = {}
    api_mock.create_order = MagicMock(return_value={
        'id': order_id,
        'symbol': 'ETH/BTC',
        'info': {
            'foo': 'bar'
        }
    })
    default_conf['dry_run'] = False
    mocker.patch('freqtrade.exchange.Exchange.amount_to_precision', lambda s, x, y: y)
    mocker.patch('freqtrade.exchange.Exchange.price_to_precision', lambda s, x, y: y)
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)

    order = exchange.create_order(pair='ETH/BTC', ordertype=order_type, side="buy",
                                  amount=1, rate=200, time_in_force=time_in_force)

    assert 'id' in order
    assert 'info' in order
    assert order['id'] == order_id
    assert api_mock.create_order.call_args[0][0] == 'ETH/BTC'
    assert api_mock.create_order.call_args[0][1] == order_type
    assert api_mock.create_order.call_args[0][2] == 'buy'
    assert api_mock.create_order.call_args[0][3] == 1
    assert api_mock.create_order.call_args[0][4] is None

    api_mock.create_order.reset_mock()
    order_type = 'limit'
    order = exchange.create_order(
        pair='ETH/BTC',
        ordertype=order_type,
        side="buy",
        amount=1,
        rate=200,
        time_in_force=time_in_force)
    assert api_mock.create_order.call_args[0][0] == 'ETH/BTC'
    assert api_mock.create_order.call_args[0][1] == order_type
    assert api_mock.create_order.call_args[0][2] == 'buy'
    assert api_mock.create_order.call_args[0][3] == 1
    assert api_mock.create_order.call_args[0][4] == 200

    # test exception handling
    with pytest.raises(DependencyException):
        api_mock.create_order = MagicMock(side_effect=ccxt.InsufficientFunds("Not enough funds"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.create_order(pair='ETH/BTC', ordertype=order_type, side="buy",
                              amount=1, rate=200, time_in_force=time_in_force)

    with pytest.raises(DependencyException):
        api_mock.create_order = MagicMock(side_effect=ccxt.InvalidOrder("Order not found"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.create_order(pair='ETH/BTC', ordertype='limit', side="buy",
                              amount=1, rate=200, time_in_force=time_in_force)

    with pytest.raises(DependencyException):
        api_mock.create_order = MagicMock(side_effect=ccxt.InvalidOrder("Order not found"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.create_order(pair='ETH/BTC', ordertype='market', side="buy",
                              amount=1, rate=200, time_in_force=time_in_force)

    with pytest.raises(TemporaryError):
        api_mock.create_order = MagicMock(side_effect=ccxt.NetworkError("Network disconnect"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.create_order(pair='ETH/BTC', ordertype=order_type, side="buy",
                              amount=1, rate=200, time_in_force=time_in_force)

    with pytest.raises(OperationalException):
        api_mock.create_order = MagicMock(side_effect=ccxt.BaseError("Unknown error"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.create_order(pair='ETH/BTC', ordertype=order_type, side="buy",
                              amount=1, rate=200, time_in_force=time_in_force)


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_buy_considers_time_in_force(default_conf, mocker, exchange_name):
    api_mock = MagicMock()
    order_id = 'test_prod_buy_{}'.format(randint(0, 10 ** 6))
    api_mock.options = {}
    api_mock.create_order = MagicMock(return_value={
        'id': order_id,
        'symbol': 'ETH/BTC',
        'info': {
            'foo': 'bar'
        }
    })
    default_conf['dry_run'] = False
    mocker.patch('freqtrade.exchange.Exchange.amount_to_precision', lambda s, x, y: y)
    mocker.patch('freqtrade.exchange.Exchange.price_to_precision', lambda s, x, y: y)
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)

    order_type = 'limit'
    time_in_force = 'ioc'

    order = exchange.create_order(pair='ETH/BTC', ordertype=order_type, side="buy",
                                  amount=1, rate=200, time_in_force=time_in_force)

    assert 'id' in order
    assert 'info' in order
    assert order['id'] == order_id
    assert api_mock.create_order.call_args[0][0] == 'ETH/BTC'
    assert api_mock.create_order.call_args[0][1] == order_type
    assert api_mock.create_order.call_args[0][2] == 'buy'
    assert api_mock.create_order.call_args[0][3] == 1
    assert api_mock.create_order.call_args[0][4] == 200
    assert "timeInForce" in api_mock.create_order.call_args[0][5]
    assert api_mock.create_order.call_args[0][5]["timeInForce"] == time_in_force

    order_type = 'market'
    time_in_force = 'ioc'

    order = exchange.create_order(pair='ETH/BTC', ordertype=order_type, side="buy",
                                  amount=1, rate=200, time_in_force=time_in_force)

    assert 'id' in order
    assert 'info' in order
    assert order['id'] == order_id
    assert api_mock.create_order.call_args[0][0] == 'ETH/BTC'
    assert api_mock.create_order.call_args[0][1] == order_type
    assert api_mock.create_order.call_args[0][2] == 'buy'
    assert api_mock.create_order.call_args[0][3] == 1
    assert api_mock.create_order.call_args[0][4] is None
    # Market orders should not send timeInForce!!
    assert "timeInForce" not in api_mock.create_order.call_args[0][5]


def test_sell_dry_run(default_conf, mocker):
    default_conf['dry_run'] = True
    exchange = get_patched_exchange(mocker, default_conf)

    order = exchange.create_order(pair='ETH/BTC', ordertype='limit',
                                  side="sell", amount=1, rate=200)
    assert 'id' in order
    assert 'dry_run_sell_' in order['id']


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_sell_prod(default_conf, mocker, exchange_name):
    api_mock = MagicMock()
    order_id = 'test_prod_sell_{}'.format(randint(0, 10 ** 6))
    order_type = 'market'
    api_mock.options = {}
    api_mock.create_order = MagicMock(return_value={
        'id': order_id,
        'symbol': 'ETH/BTC',
        'info': {
            'foo': 'bar'
        }
    })
    default_conf['dry_run'] = False

    mocker.patch('freqtrade.exchange.Exchange.amount_to_precision', lambda s, x, y: y)
    mocker.patch('freqtrade.exchange.Exchange.price_to_precision', lambda s, x, y: y)
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)

    order = exchange.create_order(pair='ETH/BTC', ordertype=order_type,
                                  side="sell", amount=1, rate=200)

    assert 'id' in order
    assert 'info' in order
    assert order['id'] == order_id
    assert api_mock.create_order.call_args[0][0] == 'ETH/BTC'
    assert api_mock.create_order.call_args[0][1] == order_type
    assert api_mock.create_order.call_args[0][2] == 'sell'
    assert api_mock.create_order.call_args[0][3] == 1
    assert api_mock.create_order.call_args[0][4] is None

    api_mock.create_order.reset_mock()
    order_type = 'limit'
    order = exchange.create_order(pair='ETH/BTC', ordertype=order_type,
                                  side="sell", amount=1, rate=200)
    assert api_mock.create_order.call_args[0][0] == 'ETH/BTC'
    assert api_mock.create_order.call_args[0][1] == order_type
    assert api_mock.create_order.call_args[0][2] == 'sell'
    assert api_mock.create_order.call_args[0][3] == 1
    assert api_mock.create_order.call_args[0][4] == 200

    # test exception handling
    with pytest.raises(DependencyException):
        api_mock.create_order = MagicMock(side_effect=ccxt.InsufficientFunds("0 balance"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.create_order(pair='ETH/BTC', ordertype=order_type, side="sell", amount=1, rate=200)

    with pytest.raises(DependencyException):
        api_mock.create_order = MagicMock(side_effect=ccxt.InvalidOrder("Order not found"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.create_order(pair='ETH/BTC', ordertype='limit', side="sell", amount=1, rate=200)

    # Market orders don't require price, so the behaviour is slightly different
    with pytest.raises(DependencyException):
        api_mock.create_order = MagicMock(side_effect=ccxt.InvalidOrder("Order not found"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.create_order(pair='ETH/BTC', ordertype='market', side="sell", amount=1, rate=200)

    with pytest.raises(TemporaryError):
        api_mock.create_order = MagicMock(side_effect=ccxt.NetworkError("No Connection"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.create_order(pair='ETH/BTC', ordertype=order_type, side="sell", amount=1, rate=200)

    with pytest.raises(OperationalException):
        api_mock.create_order = MagicMock(side_effect=ccxt.BaseError("DeadBeef"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.create_order(pair='ETH/BTC', ordertype=order_type, side="sell", amount=1, rate=200)


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_sell_considers_time_in_force(default_conf, mocker, exchange_name):
    api_mock = MagicMock()
    order_id = 'test_prod_sell_{}'.format(randint(0, 10 ** 6))
    api_mock.create_order = MagicMock(return_value={
        'id': order_id,
        'symbol': 'ETH/BTC',
        'info': {
            'foo': 'bar'
        }
    })
    api_mock.options = {}
    default_conf['dry_run'] = False
    mocker.patch('freqtrade.exchange.Exchange.amount_to_precision', lambda s, x, y: y)
    mocker.patch('freqtrade.exchange.Exchange.price_to_precision', lambda s, x, y: y)
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)

    order_type = 'limit'
    time_in_force = 'ioc'

    order = exchange.create_order(pair='ETH/BTC', ordertype=order_type, side="sell",
                                  amount=1, rate=200, time_in_force=time_in_force)

    assert 'id' in order
    assert 'info' in order
    assert order['id'] == order_id
    assert api_mock.create_order.call_args[0][0] == 'ETH/BTC'
    assert api_mock.create_order.call_args[0][1] == order_type
    assert api_mock.create_order.call_args[0][2] == 'sell'
    assert api_mock.create_order.call_args[0][3] == 1
    assert api_mock.create_order.call_args[0][4] == 200
    assert "timeInForce" in api_mock.create_order.call_args[0][5]
    assert api_mock.create_order.call_args[0][5]["timeInForce"] == time_in_force

    order_type = 'market'
    time_in_force = 'ioc'
    order = exchange.create_order(pair='ETH/BTC', ordertype=order_type, side="sell",
                                  amount=1, rate=200, time_in_force=time_in_force)

    assert 'id' in order
    assert 'info' in order
    assert order['id'] == order_id
    assert api_mock.create_order.call_args[0][0] == 'ETH/BTC'
    assert api_mock.create_order.call_args[0][1] == order_type
    assert api_mock.create_order.call_args[0][2] == 'sell'
    assert api_mock.create_order.call_args[0][3] == 1
    assert api_mock.create_order.call_args[0][4] is None
    # Market orders should not send timeInForce!!
    assert "timeInForce" not in api_mock.create_order.call_args[0][5]


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_get_balances_prod(default_conf, mocker, exchange_name):
    balance_item = {
        'free': 10.0,
        'total': 10.0,
        'used': 0.0
    }

    api_mock = MagicMock()
    api_mock.fetch_balance = MagicMock(return_value={
        '1ST': balance_item,
        '2ST': balance_item,
        '3ST': balance_item
    })
    default_conf['dry_run'] = False
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    assert len(exchange.get_balances()) == 3
    assert exchange.get_balances()['1ST']['free'] == 10.0
    assert exchange.get_balances()['1ST']['total'] == 10.0
    assert exchange.get_balances()['1ST']['used'] == 0.0

    ccxt_exceptionhandlers(mocker, default_conf, api_mock, exchange_name,
                           "get_balances", "fetch_balance")


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_get_tickers(default_conf, mocker, exchange_name):
    api_mock = MagicMock()
    tick = {'ETH/BTC': {
        'symbol': 'ETH/BTC',
        'bid': 0.5,
        'ask': 1,
        'last': 42,
    }, 'BCH/BTC': {
        'symbol': 'BCH/BTC',
        'bid': 0.6,
        'ask': 0.5,
        'last': 41,
    }
    }
    api_mock.fetch_tickers = MagicMock(return_value=tick)
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    # retrieve original ticker
    tickers = exchange.get_tickers()

    assert 'ETH/BTC' in tickers
    assert 'BCH/BTC' in tickers
    assert tickers['ETH/BTC']['bid'] == 0.5
    assert tickers['ETH/BTC']['ask'] == 1
    assert tickers['BCH/BTC']['bid'] == 0.6
    assert tickers['BCH/BTC']['ask'] == 0.5
    assert api_mock.fetch_tickers.call_count == 1

    api_mock.fetch_tickers.reset_mock()

    # Cached ticker should not call api again
    tickers2 = exchange.get_tickers(cached=True)
    assert tickers2 == tickers
    assert api_mock.fetch_tickers.call_count == 0
    tickers2 = exchange.get_tickers(cached=False)
    assert api_mock.fetch_tickers.call_count == 1

    ccxt_exceptionhandlers(mocker, default_conf, api_mock, exchange_name,
                           "get_tickers", "fetch_tickers")

    with pytest.raises(OperationalException):
        api_mock.fetch_tickers = MagicMock(side_effect=ccxt.NotSupported("DeadBeef"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.get_tickers()

    api_mock.fetch_tickers = MagicMock(return_value={})
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    exchange.get_tickers()


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_fetch_ticker(default_conf, mocker, exchange_name):
    api_mock = MagicMock()
    tick = {
        'symbol': 'ETH/BTC',
        'bid': 0.00001098,
        'ask': 0.00001099,
        'last': 0.0001,
    }
    api_mock.fetch_ticker = MagicMock(return_value=tick)
    api_mock.markets = {'ETH/BTC': {'active': True}}
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    # retrieve original ticker
    ticker = exchange.fetch_ticker(pair='ETH/BTC')

    assert ticker['bid'] == 0.00001098
    assert ticker['ask'] == 0.00001099

    # change the ticker
    tick = {
        'symbol': 'ETH/BTC',
        'bid': 0.5,
        'ask': 1,
        'last': 42,
    }
    api_mock.fetch_ticker = MagicMock(return_value=tick)
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)

    # if not caching the result we should get the same ticker
    # if not fetching a new result we should get the cached ticker
    ticker = exchange.fetch_ticker(pair='ETH/BTC')

    assert api_mock.fetch_ticker.call_count == 1
    assert ticker['bid'] == 0.5
    assert ticker['ask'] == 1

    ccxt_exceptionhandlers(mocker, default_conf, api_mock, exchange_name,
                           "fetch_ticker", "fetch_ticker",
                           pair='ETH/BTC')

    api_mock.fetch_ticker = MagicMock(return_value={})
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    exchange.fetch_ticker(pair='ETH/BTC')

    with pytest.raises(DependencyException, match=r'Pair XRP/ETH not available'):
        exchange.fetch_ticker(pair='XRP/ETH')


@pytest.mark.parametrize("exchange_name", EXCHANGES)
@pytest.mark.parametrize('candle_type', ['mark', ''])
def test_get_historic_ohlcv(default_conf, mocker, caplog, exchange_name, candle_type):
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    ohlcv = [
        [
            arrow.utcnow().int_timestamp * 1000,  # unix timestamp ms
            1,  # open
            2,  # high
            3,  # low
            4,  # close
            5,  # volume (in quote currency)
        ]
    ]
    pair = 'ETH/BTC'

    async def mock_candle_hist(pair, timeframe, candle_type, since_ms):
        return pair, timeframe, candle_type, ohlcv

    exchange._async_get_candle_history = Mock(wraps=mock_candle_hist)
    # one_call calculation * 1.8 should do 2 calls

    since = 5 * 60 * exchange.ohlcv_candle_limit('5m') * 1.8
    ret = exchange.get_historic_ohlcv(
        pair,
        "5m",
        int((arrow.utcnow().int_timestamp - since) * 1000),
        candle_type=candle_type
    )

    assert exchange._async_get_candle_history.call_count == 2
    # Returns twice the above OHLCV data
    assert len(ret) == 2
    assert log_has_re(r'Downloaded data for .* with length .*\.', caplog)

    caplog.clear()

    async def mock_get_candle_hist_error(pair, *args, **kwargs):
        raise TimeoutError()

    exchange._async_get_candle_history = MagicMock(side_effect=mock_get_candle_hist_error)
    ret = exchange.get_historic_ohlcv(
        pair,
        "5m",
        int((arrow.utcnow().int_timestamp - since) * 1000),
        candle_type=candle_type
    )
    assert log_has_re(r"Async code raised an exception: .*", caplog)


@pytest.mark.parametrize("exchange_name", EXCHANGES)
@pytest.mark.parametrize('candle_type', ['mark', ''])
def test_get_historic_ohlcv_as_df(default_conf, mocker, exchange_name, candle_type):
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    ohlcv = [
        [
            arrow.utcnow().int_timestamp * 1000,  # unix timestamp ms
            1,  # open
            2,  # high
            3,  # low
            4,  # close
            5,  # volume (in quote currency)
        ],
        [
            arrow.utcnow().shift(minutes=5).int_timestamp * 1000,  # unix timestamp ms
            1,  # open
            2,  # high
            3,  # low
            4,  # close
            5,  # volume (in quote currency)
        ],
        [
            arrow.utcnow().shift(minutes=10).int_timestamp * 1000,  # unix timestamp ms
            1,  # open
            2,  # high
            3,  # low
            4,  # close
            5,  # volume (in quote currency)
        ]
    ]
    pair = 'ETH/BTC'

    async def mock_candle_hist(pair, timeframe, candle_type, since_ms):
        return pair, timeframe, candle_type, ohlcv

    exchange._async_get_candle_history = Mock(wraps=mock_candle_hist)
    # one_call calculation * 1.8 should do 2 calls

    since = 5 * 60 * exchange.ohlcv_candle_limit('5m') * 1.8
    ret = exchange.get_historic_ohlcv_as_df(
        pair,
        "5m",
        int((arrow.utcnow().int_timestamp - since) * 1000),
        candle_type=candle_type
    )

    assert exchange._async_get_candle_history.call_count == 2
    # Returns twice the above OHLCV data
    assert len(ret) == 2
    assert isinstance(ret, DataFrame)
    assert 'date' in ret.columns
    assert 'open' in ret.columns
    assert 'close' in ret.columns
    assert 'high' in ret.columns


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange_name", EXCHANGES)
@pytest.mark.parametrize('candle_type', [CandleType.MARK, CandleType.SPOT])
async def test__async_get_historic_ohlcv(default_conf, mocker, caplog, exchange_name, candle_type):
    ohlcv = [
        [
            int((datetime.now(timezone.utc).timestamp() - 1000) * 1000),
            1,  # open
            2,  # high
            3,  # low
            4,  # close
            5,  # volume (in quote currency)
        ]
    ]
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    # Monkey-patch async function
    exchange._api_async.fetch_ohlcv = get_mock_coro(ohlcv)

    pair = 'ETH/USDT'
    respair, restf, _, res = await exchange._async_get_historic_ohlcv(
        pair, "5m", 1500000000000, candle_type=candle_type, is_new_pair=False)
    assert respair == pair
    assert restf == '5m'
    # Call with very old timestamp - causes tons of requests
    assert exchange._api_async.fetch_ohlcv.call_count > 200
    assert res[0] == ohlcv[0]


@pytest.mark.parametrize('candle_type', [CandleType.FUTURES, CandleType.MARK, CandleType.SPOT])
def test_refresh_latest_ohlcv(mocker, default_conf, caplog, candle_type) -> None:
    ohlcv = [
        [
            (arrow.utcnow().int_timestamp - 1) * 1000,  # unix timestamp ms
            1,  # open
            2,  # high
            3,  # low
            4,  # close
            5,  # volume (in quote currency)
        ],
        [
            arrow.utcnow().int_timestamp * 1000,  # unix timestamp ms
            3,  # open
            1,  # high
            4,  # low
            6,  # close
            5,  # volume (in quote currency)
        ]
    ]

    caplog.set_level(logging.DEBUG)
    exchange = get_patched_exchange(mocker, default_conf)
    exchange._api_async.fetch_ohlcv = get_mock_coro(ohlcv)

    pairs = [('IOTA/ETH', '5m', candle_type), ('XRP/ETH', '5m', candle_type)]
    # empty dicts
    assert not exchange._klines
    res = exchange.refresh_latest_ohlcv(pairs, cache=False)
    # No caching
    assert not exchange._klines

    assert len(res) == len(pairs)
    assert exchange._api_async.fetch_ohlcv.call_count == 2
    exchange._api_async.fetch_ohlcv.reset_mock()

    exchange.required_candle_call_count = 2
    res = exchange.refresh_latest_ohlcv(pairs)
    assert len(res) == len(pairs)

    assert log_has(f'Refreshing candle (OHLCV) data for {len(pairs)} pairs', caplog)
    assert exchange._klines
    assert exchange._api_async.fetch_ohlcv.call_count == 4
    exchange._api_async.fetch_ohlcv.reset_mock()
    for pair in pairs:
        assert isinstance(exchange.klines(pair), DataFrame)
        assert len(exchange.klines(pair)) > 0

        # klines function should return a different object on each call
        # if copy is "True"
        assert exchange.klines(pair) is not exchange.klines(pair)
        assert exchange.klines(pair) is not exchange.klines(pair, copy=True)
        assert exchange.klines(pair, copy=True) is not exchange.klines(pair, copy=True)
        assert exchange.klines(pair, copy=False) is exchange.klines(pair, copy=False)

    # test caching
    res = exchange.refresh_latest_ohlcv(
        [('IOTA/ETH', '5m', candle_type), ('XRP/ETH', '5m', candle_type)])
    assert len(res) == len(pairs)

    assert exchange._api_async.fetch_ohlcv.call_count == 0
    exchange.required_candle_call_count = 1
    assert log_has(f"Using cached candle (OHLCV) data for {pairs[0][0]}, "
                   f"{pairs[0][1]}, {candle_type} ...",
                   caplog)
    pairlist = [
        ('IOTA/ETH', '5m', candle_type),
        ('XRP/ETH', '5m', candle_type),
        ('XRP/ETH', '1d', candle_type)]
    res = exchange.refresh_latest_ohlcv(pairlist, cache=False)
    assert len(res) == 3
    assert exchange._api_async.fetch_ohlcv.call_count == 3

    # Test the same again, should NOT return from cache!
    exchange._api_async.fetch_ohlcv.reset_mock()
    res = exchange.refresh_latest_ohlcv(pairlist, cache=False)
    assert len(res) == 3
    assert exchange._api_async.fetch_ohlcv.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange_name", EXCHANGES)
async def test__async_get_candle_history(default_conf, mocker, caplog, exchange_name):
    ohlcv = [
        [
            arrow.utcnow().int_timestamp * 1000,  # unix timestamp ms
            1,  # open
            2,  # high
            3,  # low
            4,  # close
            5,  # volume (in quote currency)
        ]
    ]

    caplog.set_level(logging.DEBUG)
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    # Monkey-patch async function
    exchange._api_async.fetch_ohlcv = get_mock_coro(ohlcv)

    pair = 'ETH/BTC'
    res = await exchange._async_get_candle_history(pair, "5m", CandleType.SPOT)
    assert type(res) is tuple
    assert len(res) == 4
    assert res[0] == pair
    assert res[1] == "5m"
    assert res[2] == CandleType.SPOT
    assert res[3] == ohlcv
    assert exchange._api_async.fetch_ohlcv.call_count == 1
    assert not log_has(f"Using cached candle (OHLCV) data for {pair} ...", caplog)

    # exchange = Exchange(default_conf)
    await async_ccxt_exception(mocker, default_conf, MagicMock(),
                               "_async_get_candle_history", "fetch_ohlcv",
                               pair='ABCD/BTC', timeframe=default_conf['timeframe'],
                               candle_type=CandleType.SPOT)

    api_mock = MagicMock()
    with pytest.raises(OperationalException,
                       match=r'Could not fetch historical candle \(OHLCV\) data.*'):
        api_mock.fetch_ohlcv = MagicMock(side_effect=ccxt.BaseError("Unknown error"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        await exchange._async_get_candle_history(pair, "5m", CandleType.SPOT,
                                                 (arrow.utcnow().int_timestamp - 2000) * 1000)

    with pytest.raises(OperationalException, match=r'Exchange.* does not support fetching '
                                                   r'historical candle \(OHLCV\) data\..*'):
        api_mock.fetch_ohlcv = MagicMock(side_effect=ccxt.NotSupported("Not supported"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        await exchange._async_get_candle_history(pair, "5m", CandleType.SPOT,
                                                 (arrow.utcnow().int_timestamp - 2000) * 1000)


@pytest.mark.asyncio
async def test__async_kucoin_get_candle_history(default_conf, mocker, caplog):
    caplog.set_level(logging.INFO)
    api_mock = MagicMock()
    api_mock.fetch_ohlcv = MagicMock(side_effect=ccxt.DDoSProtection(
        "kucoin GET https://openapi-v2.kucoin.com/api/v1/market/candles?"
        "symbol=ETH-BTC&type=5min&startAt=1640268735&endAt=1640418735"
        "429 Too Many Requests" '{"code":"429000","msg":"Too Many Requests"}'))
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id="kucoin")

    msg = "Kucoin 429 error, avoid triggering DDosProtection backoff delay"
    assert not num_log_has_re(msg, caplog)

    for _ in range(3):
        with pytest.raises(DDosProtection, match=r'429 Too Many Requests'):
            await exchange._async_get_candle_history(
                "ETH/BTC", "5m", (arrow.utcnow().int_timestamp - 2000) * 1000, count=3)
    assert num_log_has_re(msg, caplog) == 3

    caplog.clear()
    # Test regular non-kucoin message
    api_mock.fetch_ohlcv = MagicMock(side_effect=ccxt.DDoSProtection(
        "kucoin GET https://openapi-v2.kucoin.com/api/v1/market/candles?"
        "symbol=ETH-BTC&type=5min&startAt=1640268735&endAt=1640418735"
        "429 Too Many Requests" '{"code":"2222222","msg":"Too Many Requests"}'))

    msg = r'_async_get_candle_history\(\) returned exception: .*'
    msg2 = r'Applying DDosProtection backoff delay: .*'
    with patch('freqtrade.exchange.common.asyncio.sleep', get_mock_coro(None)):
        for _ in range(3):
            with pytest.raises(DDosProtection, match=r'429 Too Many Requests'):
                await exchange._async_get_candle_history(
                    "ETH/BTC", "5m", (arrow.utcnow().int_timestamp - 2000) * 1000, count=3)
        # Expect the "returned exception" message 12 times (4 retries * 3 (loop))
        assert num_log_has_re(msg, caplog) == 12
        assert num_log_has_re(msg2, caplog) == 9


@pytest.mark.asyncio
async def test__async_get_candle_history_empty(default_conf, mocker, caplog):
    """ Test empty exchange result """
    ohlcv = []

    caplog.set_level(logging.DEBUG)
    exchange = get_patched_exchange(mocker, default_conf)
    # Monkey-patch async function
    exchange._api_async.fetch_ohlcv = get_mock_coro([])

    exchange = Exchange(default_conf)
    pair = 'ETH/BTC'
    res = await exchange._async_get_candle_history(pair, "5m", CandleType.SPOT)
    assert type(res) is tuple
    assert len(res) == 4
    assert res[0] == pair
    assert res[1] == "5m"
    assert res[2] == CandleType.SPOT
    assert res[3] == ohlcv
    assert exchange._api_async.fetch_ohlcv.call_count == 1


def test_refresh_latest_ohlcv_inv_result(default_conf, mocker, caplog):

    async def mock_get_candle_hist(pair, *args, **kwargs):
        if pair == 'ETH/BTC':
            return [[]]
        else:
            raise TypeError()

    exchange = get_patched_exchange(mocker, default_conf)

    # Monkey-patch async function with empty result
    exchange._api_async.fetch_ohlcv = MagicMock(side_effect=mock_get_candle_hist)

    pairs = [("ETH/BTC", "5m", ''), ("XRP/BTC", "5m", '')]
    res = exchange.refresh_latest_ohlcv(pairs)
    assert exchange._klines
    assert exchange._api_async.fetch_ohlcv.call_count == 2

    assert type(res) is dict
    assert len(res) == 1
    # Test that each is in list at least once as order is not guaranteed
    assert log_has("Error loading ETH/BTC. Result was [[]].", caplog)
    assert log_has("Async code raised an exception: TypeError()", caplog)


def test_get_next_limit_in_list():
    limit_range = [5, 10, 20, 50, 100, 500, 1000]
    assert Exchange.get_next_limit_in_list(1, limit_range) == 5
    assert Exchange.get_next_limit_in_list(5, limit_range) == 5
    assert Exchange.get_next_limit_in_list(6, limit_range) == 10
    assert Exchange.get_next_limit_in_list(9, limit_range) == 10
    assert Exchange.get_next_limit_in_list(10, limit_range) == 10
    assert Exchange.get_next_limit_in_list(11, limit_range) == 20
    assert Exchange.get_next_limit_in_list(19, limit_range) == 20
    assert Exchange.get_next_limit_in_list(21, limit_range) == 50
    assert Exchange.get_next_limit_in_list(51, limit_range) == 100
    assert Exchange.get_next_limit_in_list(1000, limit_range) == 1000
    # Going over the limit ...
    assert Exchange.get_next_limit_in_list(1001, limit_range) == 1000
    assert Exchange.get_next_limit_in_list(2000, limit_range) == 1000
    # Without required range
    assert Exchange.get_next_limit_in_list(2000, limit_range, False) is None
    assert Exchange.get_next_limit_in_list(15, limit_range, False) == 20

    assert Exchange.get_next_limit_in_list(21, None) == 21
    assert Exchange.get_next_limit_in_list(100, None) == 100
    assert Exchange.get_next_limit_in_list(1000, None) == 1000


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_fetch_l2_order_book(default_conf, mocker, order_book_l2, exchange_name):
    default_conf['exchange']['name'] = exchange_name
    api_mock = MagicMock()

    api_mock.fetch_l2_order_book = order_book_l2
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    order_book = exchange.fetch_l2_order_book(pair='ETH/BTC', limit=10)
    assert 'bids' in order_book
    assert 'asks' in order_book
    assert len(order_book['bids']) == 10
    assert len(order_book['asks']) == 10
    assert api_mock.fetch_l2_order_book.call_args_list[0][0][0] == 'ETH/BTC'

    for val in [1, 5, 10, 12, 20, 50, 100]:
        api_mock.fetch_l2_order_book.reset_mock()

        order_book = exchange.fetch_l2_order_book(pair='ETH/BTC', limit=val)
        assert api_mock.fetch_l2_order_book.call_args_list[0][0][0] == 'ETH/BTC'
        # Not all exchanges support all limits for orderbook
        if not exchange._ft_has['l2_limit_range'] or val in exchange._ft_has['l2_limit_range']:
            assert api_mock.fetch_l2_order_book.call_args_list[0][0][1] == val
        else:
            next_limit = exchange.get_next_limit_in_list(val, exchange._ft_has['l2_limit_range'])
            assert api_mock.fetch_l2_order_book.call_args_list[0][0][1] == next_limit


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_fetch_l2_order_book_exception(default_conf, mocker, exchange_name):
    api_mock = MagicMock()
    with pytest.raises(OperationalException):
        api_mock.fetch_l2_order_book = MagicMock(side_effect=ccxt.NotSupported("Not supported"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.fetch_l2_order_book(pair='ETH/BTC', limit=50)
    with pytest.raises(TemporaryError):
        api_mock.fetch_l2_order_book = MagicMock(side_effect=ccxt.NetworkError("DeadBeef"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.fetch_l2_order_book(pair='ETH/BTC', limit=50)
    with pytest.raises(OperationalException):
        api_mock.fetch_l2_order_book = MagicMock(side_effect=ccxt.BaseError("DeadBeef"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.fetch_l2_order_book(pair='ETH/BTC', limit=50)


@pytest.mark.parametrize("side,ask,bid,last,last_ab,expected", [
    ('ask', 20, 19, 10, 0.0, 20),  # Full ask side
    ('ask', 20, 19, 10, 1.0, 10),  # Full last side
    ('ask', 20, 19, 10, 0.5, 15),  # Between ask and last
    ('ask', 20, 19, 10, 0.7, 13),  # Between ask and last
    ('ask', 20, 19, 10, 0.3, 17),  # Between ask and last
    ('ask', 5, 6, 10, 1.0, 5),  # last bigger than ask
    ('ask', 5, 6, 10, 0.5, 5),  # last bigger than ask
    ('ask', 20, 19, 10, None, 20),  # ask_last_balance missing
    ('ask', 10, 20, None, 0.5, 10),  # last not available - uses ask
    ('ask', 4, 5, None, 0.5, 4),  # last not available - uses ask
    ('ask', 4, 5, None, 1, 4),  # last not available - uses ask
    ('ask', 4, 5, None, 0, 4),  # last not available - uses ask
    ('bid', 21, 20, 10, 0.0, 20),  # Full bid side
    ('bid', 21, 20, 10, 1.0, 10),  # Full last side
    ('bid', 21, 20, 10, 0.5, 15),  # Between bid and last
    ('bid', 21, 20, 10, 0.7, 13),  # Between bid and last
    ('bid', 21, 20, 10, 0.3, 17),  # Between bid and last
    ('bid', 6, 5, 10, 1.0, 5),  # last bigger than bid
    ('bid', 21, 20, 10, None, 20),  # ask_last_balance missing
    ('bid', 6, 5, 10, 0.5, 5),  # last bigger than bid
    ('bid', 21, 20, None, 0.5, 20),  # last not available - uses bid
    ('bid', 6, 5, None, 0.5, 5),  # last not available - uses bid
    ('bid', 6, 5, None, 1, 5),  # last not available - uses bid
    ('bid', 6, 5, None, 0, 5),  # last not available - uses bid
])
def test_get_buy_rate(mocker, default_conf, caplog, side, ask, bid,
                      last, last_ab, expected) -> None:
    caplog.set_level(logging.DEBUG)
    if last_ab is None:
        del default_conf['bid_strategy']['ask_last_balance']
    else:
        default_conf['bid_strategy']['ask_last_balance'] = last_ab
    default_conf['bid_strategy']['price_side'] = side
    exchange = get_patched_exchange(mocker, default_conf)
    mocker.patch('freqtrade.exchange.Exchange.fetch_ticker',
                 return_value={'ask': ask, 'last': last, 'bid': bid})

    assert exchange.get_rate('ETH/BTC', refresh=True, side="buy") == expected
    assert not log_has("Using cached buy rate for ETH/BTC.", caplog)

    assert exchange.get_rate('ETH/BTC', refresh=False, side="buy") == expected
    assert log_has("Using cached buy rate for ETH/BTC.", caplog)
    # Running a 2nd time with Refresh on!
    caplog.clear()
    assert exchange.get_rate('ETH/BTC', refresh=True, side="buy") == expected
    assert not log_has("Using cached buy rate for ETH/BTC.", caplog)


@pytest.mark.parametrize('side,ask,bid,last,last_ab,expected', [
    ('bid', 12.0, 11.0, 11.5, 0.0, 11.0),  # full bid side
    ('bid', 12.0, 11.0, 11.5, 1.0, 11.5),  # full last side
    ('bid', 12.0, 11.0, 11.5, 0.5, 11.25),  # between bid and lat
    ('bid', 12.0, 11.2, 10.5, 0.0, 11.2),  # Last smaller than bid
    ('bid', 12.0, 11.2, 10.5, 1.0, 11.2),  # Last smaller than bid - uses bid
    ('bid', 12.0, 11.2, 10.5, 0.5, 11.2),  # Last smaller than bid - uses bid
    ('bid', 0.003, 0.002, 0.005, 0.0, 0.002),
    ('bid', 0.003, 0.002, 0.005, None, 0.002),
    ('ask', 12.0, 11.0, 12.5, 0.0, 12.0),  # full ask side
    ('ask', 12.0, 11.0, 12.5, 1.0, 12.5),  # full last side
    ('ask', 12.0, 11.0, 12.5, 0.5, 12.25),  # between bid and lat
    ('ask', 12.2, 11.2, 10.5, 0.0, 12.2),  # Last smaller than ask
    ('ask', 12.0, 11.0, 10.5, 1.0, 12.0),  # Last smaller than ask - uses ask
    ('ask', 12.0, 11.2, 10.5, 0.5, 12.0),  # Last smaller than ask - uses ask
    ('ask', 10.0, 11.0, 11.0, 0.0, 10.0),
    ('ask', 10.11, 11.2, 11.0, 0.0, 10.11),
    ('ask', 0.001, 0.002, 11.0, 0.0, 0.001),
    ('ask', 0.006, 1.0, 11.0, 0.0, 0.006),
    ('ask', 0.006, 1.0, 11.0, None, 0.006),
])
def test_get_sell_rate(default_conf, mocker, caplog, side, bid, ask,
                       last, last_ab, expected) -> None:
    caplog.set_level(logging.DEBUG)

    default_conf['ask_strategy']['price_side'] = side
    if last_ab is not None:
        default_conf['ask_strategy']['bid_last_balance'] = last_ab
    mocker.patch('freqtrade.exchange.Exchange.fetch_ticker',
                 return_value={'ask': ask, 'bid': bid, 'last': last})
    pair = "ETH/BTC"

    # Test regular mode
    exchange = get_patched_exchange(mocker, default_conf)
    rate = exchange.get_rate(pair, refresh=True, side="sell")
    assert not log_has("Using cached sell rate for ETH/BTC.", caplog)
    assert isinstance(rate, float)
    assert rate == expected
    # Use caching
    rate = exchange.get_rate(pair, refresh=False, side="sell")
    assert rate == expected
    assert log_has("Using cached sell rate for ETH/BTC.", caplog)


@pytest.mark.parametrize("entry,side,ask,bid,last,last_ab,expected", [
    ('buy', 'ask', None, 4, 4,  0, 4),  # ask not available
    ('buy', 'ask', None, None, 4,  0, 4),  # ask not available
    ('buy', 'bid', 6, None, 4,  0, 5),  # bid not available
    ('buy', 'bid', None, None, 4,  0, 5),  # No rate available
    ('sell', 'ask', None, 4, 4,  0, 4),  # ask not available
    ('sell', 'ask', None, None, 4,  0, 4),  # ask not available
    ('sell', 'bid', 6, None, 4,  0, 5),  # bid not available
    ('sell', 'bid', None, None, 4,  0, 5),  # bid not available
])
def test_get_ticker_rate_error(mocker, entry, default_conf, caplog, side, ask, bid,
                               last, last_ab, expected) -> None:
    caplog.set_level(logging.DEBUG)
    default_conf['bid_strategy']['ask_last_balance'] = last_ab
    default_conf['bid_strategy']['price_side'] = side
    default_conf['ask_strategy']['price_side'] = side
    default_conf['ask_strategy']['ask_last_balance'] = last_ab
    exchange = get_patched_exchange(mocker, default_conf)
    mocker.patch('freqtrade.exchange.Exchange.fetch_ticker',
                 return_value={'ask': ask, 'last': last, 'bid': bid})

    with pytest.raises(PricingError):
        exchange.get_rate('ETH/BTC', refresh=True, side=entry)


@pytest.mark.parametrize('side,expected', [
    ('bid', 0.043936),  # Value from order_book_l2 fiture - bids side
    ('ask', 0.043949),  # Value from order_book_l2 fiture - asks side
])
def test_get_sell_rate_orderbook(default_conf, mocker, caplog, side, expected, order_book_l2):
    caplog.set_level(logging.DEBUG)
    # Test orderbook mode
    default_conf['ask_strategy']['price_side'] = side
    default_conf['ask_strategy']['use_order_book'] = True
    default_conf['ask_strategy']['order_book_top'] = 1
    pair = "ETH/BTC"
    mocker.patch('freqtrade.exchange.Exchange.fetch_l2_order_book', order_book_l2)
    exchange = get_patched_exchange(mocker, default_conf)
    rate = exchange.get_rate(pair, refresh=True, side="sell")
    assert not log_has("Using cached sell rate for ETH/BTC.", caplog)
    assert isinstance(rate, float)
    assert rate == expected
    rate = exchange.get_rate(pair, refresh=False, side="sell")
    assert rate == expected
    assert log_has("Using cached sell rate for ETH/BTC.", caplog)


def test_get_sell_rate_orderbook_exception(default_conf, mocker, caplog):
    # Test orderbook mode
    default_conf['ask_strategy']['price_side'] = 'ask'
    default_conf['ask_strategy']['use_order_book'] = True
    default_conf['ask_strategy']['order_book_top'] = 1
    pair = "ETH/BTC"
    # Test What happens if the exchange returns an empty orderbook.
    mocker.patch('freqtrade.exchange.Exchange.fetch_l2_order_book',
                 return_value={'bids': [[]], 'asks': [[]]})
    exchange = get_patched_exchange(mocker, default_conf)
    with pytest.raises(PricingError):
        exchange.get_rate(pair, refresh=True, side="sell")
    assert log_has_re(r"Sell Price at location 1 from orderbook could not be determined\..*",
                      caplog)


def test_get_sell_rate_exception(default_conf, mocker, caplog):
    # Ticker on one side can be empty in certain circumstances.
    default_conf['ask_strategy']['price_side'] = 'ask'
    pair = "ETH/BTC"
    mocker.patch('freqtrade.exchange.Exchange.fetch_ticker',
                 return_value={'ask': None, 'bid': 0.12, 'last': None})
    exchange = get_patched_exchange(mocker, default_conf)
    with pytest.raises(PricingError, match=r"Sell-Rate for ETH/BTC was empty."):
        exchange.get_rate(pair, refresh=True, side="sell")

    exchange._config['ask_strategy']['price_side'] = 'bid'
    assert exchange.get_rate(pair, refresh=True, side="sell") == 0.12
    # Reverse sides
    mocker.patch('freqtrade.exchange.Exchange.fetch_ticker',
                 return_value={'ask': 0.13, 'bid': None, 'last': None})
    with pytest.raises(PricingError, match=r"Sell-Rate for ETH/BTC was empty."):
        exchange.get_rate(pair, refresh=True, side="sell")

    exchange._config['ask_strategy']['price_side'] = 'ask'
    assert exchange.get_rate(pair, refresh=True, side="sell") == 0.13


@pytest.mark.parametrize("exchange_name", EXCHANGES)
@pytest.mark.asyncio
async def test___async_get_candle_history_sort(default_conf, mocker, exchange_name):
    def sort_data(data, key):
        return sorted(data, key=key)

    # GDAX use-case (real data from GDAX)
    # This OHLCV data is ordered DESC (newest first, oldest last)
    ohlcv = [
        [1527833100000, 0.07666, 0.07671, 0.07666, 0.07668, 16.65244264],
        [1527832800000, 0.07662, 0.07666, 0.07662, 0.07666, 1.30051526],
        [1527832500000, 0.07656, 0.07661, 0.07656, 0.07661, 12.034778840000001],
        [1527832200000, 0.07658, 0.07658, 0.07655, 0.07656, 0.59780186],
        [1527831900000, 0.07658, 0.07658, 0.07658, 0.07658, 1.76278136],
        [1527831600000, 0.07658, 0.07658, 0.07658, 0.07658, 2.22646521],
        [1527831300000, 0.07655, 0.07657, 0.07655, 0.07657, 1.1753],
        [1527831000000, 0.07654, 0.07654, 0.07651, 0.07651, 0.8073060299999999],
        [1527830700000, 0.07652, 0.07652, 0.07651, 0.07652, 10.04822687],
        [1527830400000, 0.07649, 0.07651, 0.07649, 0.07651, 2.5734867]
    ]
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    exchange._api_async.fetch_ohlcv = get_mock_coro(ohlcv)
    sort_mock = mocker.patch('freqtrade.exchange.exchange.sorted', MagicMock(side_effect=sort_data))
    # Test the OHLCV data sort
    res = await exchange._async_get_candle_history(
        'ETH/BTC', default_conf['timeframe'], CandleType.SPOT)
    assert res[0] == 'ETH/BTC'
    res_ohlcv = res[3]

    assert sort_mock.call_count == 1
    assert res_ohlcv[0][0] == 1527830400000
    assert res_ohlcv[0][1] == 0.07649
    assert res_ohlcv[0][2] == 0.07651
    assert res_ohlcv[0][3] == 0.07649
    assert res_ohlcv[0][4] == 0.07651
    assert res_ohlcv[0][5] == 2.5734867

    assert res_ohlcv[9][0] == 1527833100000
    assert res_ohlcv[9][1] == 0.07666
    assert res_ohlcv[9][2] == 0.07671
    assert res_ohlcv[9][3] == 0.07666
    assert res_ohlcv[9][4] == 0.07668
    assert res_ohlcv[9][5] == 16.65244264

    # Bittrex use-case (real data from Bittrex)
    # This OHLCV data is ordered ASC (oldest first, newest last)
    ohlcv = [
        [1527827700000, 0.07659999, 0.0766, 0.07627, 0.07657998, 1.85216924],
        [1527828000000, 0.07657995, 0.07657995, 0.0763, 0.0763, 26.04051037],
        [1527828300000, 0.0763, 0.07659998, 0.0763, 0.0764, 10.36434124],
        [1527828600000, 0.0764, 0.0766, 0.0764, 0.0766, 5.71044773],
        [1527828900000, 0.0764, 0.07666998, 0.0764, 0.07666998, 47.48888565],
        [1527829200000, 0.0765, 0.07672999, 0.0765, 0.07672999, 3.37640326],
        [1527829500000, 0.0766, 0.07675, 0.0765, 0.07675, 8.36203831],
        [1527829800000, 0.07675, 0.07677999, 0.07620002, 0.076695, 119.22963884],
        [1527830100000, 0.076695, 0.07671, 0.07624171, 0.07671, 1.80689244],
        [1527830400000, 0.07671, 0.07674399, 0.07629216, 0.07655213, 2.31452783]
    ]
    exchange._api_async.fetch_ohlcv = get_mock_coro(ohlcv)
    # Reset sort mock
    sort_mock = mocker.patch('freqtrade.exchange.sorted', MagicMock(side_effect=sort_data))
    # Test the OHLCV data sort
    res = await exchange._async_get_candle_history(
        'ETH/BTC', default_conf['timeframe'], CandleType.SPOT)
    assert res[0] == 'ETH/BTC'
    assert res[1] == default_conf['timeframe']
    res_ohlcv = res[3]
    # Sorted not called again - data is already in order
    assert sort_mock.call_count == 0
    assert res_ohlcv[0][0] == 1527827700000
    assert res_ohlcv[0][1] == 0.07659999
    assert res_ohlcv[0][2] == 0.0766
    assert res_ohlcv[0][3] == 0.07627
    assert res_ohlcv[0][4] == 0.07657998
    assert res_ohlcv[0][5] == 1.85216924

    assert res_ohlcv[9][0] == 1527830400000
    assert res_ohlcv[9][1] == 0.07671
    assert res_ohlcv[9][2] == 0.07674399
    assert res_ohlcv[9][3] == 0.07629216
    assert res_ohlcv[9][4] == 0.07655213
    assert res_ohlcv[9][5] == 2.31452783


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange_name", EXCHANGES)
async def test__async_fetch_trades(default_conf, mocker, caplog, exchange_name,
                                   fetch_trades_result):
    caplog.set_level(logging.DEBUG)
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    # Monkey-patch async function
    exchange._api_async.fetch_trades = get_mock_coro(fetch_trades_result)

    pair = 'ETH/BTC'
    res = await exchange._async_fetch_trades(pair, since=None, params=None)
    assert type(res) is list
    assert isinstance(res[0], list)
    assert isinstance(res[1], list)

    assert exchange._api_async.fetch_trades.call_count == 1
    assert exchange._api_async.fetch_trades.call_args[0][0] == pair
    assert exchange._api_async.fetch_trades.call_args[1]['limit'] == 1000

    assert log_has_re(f"Fetching trades for pair {pair}, since .*", caplog)
    caplog.clear()
    exchange._api_async.fetch_trades.reset_mock()
    res = await exchange._async_fetch_trades(pair, since=None, params={'from': '123'})
    assert exchange._api_async.fetch_trades.call_count == 1
    assert exchange._api_async.fetch_trades.call_args[0][0] == pair
    assert exchange._api_async.fetch_trades.call_args[1]['limit'] == 1000
    assert exchange._api_async.fetch_trades.call_args[1]['params'] == {'from': '123'}
    assert log_has_re(f"Fetching trades for pair {pair}, params: .*", caplog)

    exchange = Exchange(default_conf)
    await async_ccxt_exception(mocker, default_conf, MagicMock(),
                               "_async_fetch_trades", "fetch_trades",
                               pair='ABCD/BTC', since=None)

    api_mock = MagicMock()
    with pytest.raises(OperationalException, match=r'Could not fetch trade data*'):
        api_mock.fetch_trades = MagicMock(side_effect=ccxt.BaseError("Unknown error"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        await exchange._async_fetch_trades(pair, since=(arrow.utcnow().int_timestamp - 2000) * 1000)

    with pytest.raises(OperationalException, match=r'Exchange.* does not support fetching '
                                                   r'historical trade data\..*'):
        api_mock.fetch_trades = MagicMock(side_effect=ccxt.NotSupported("Not supported"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        await exchange._async_fetch_trades(pair, since=(arrow.utcnow().int_timestamp - 2000) * 1000)


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange_name", EXCHANGES)
async def test__async_fetch_trades_contract_size(default_conf, mocker, caplog, exchange_name,
                                                 fetch_trades_result):
    caplog.set_level(logging.DEBUG)
    default_conf['collateral'] = 'isolated'
    default_conf['trading_mode'] = 'futures'
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    # Monkey-patch async function
    exchange._api_async.fetch_trades = get_mock_coro([
        {'info': {'a': 126181333,
                  'p': '0.01952600',
                  'q': '0.01200000',
                  'f': 138604158,
                  'l': 138604158,
                  'T': 1565798399872,
                  'm': True,
                  'M': True},
         'timestamp': 1565798399872,
         'datetime': '2019-08-14T15:59:59.872Z',
         'symbol': 'ETH/USDT:USDT',
         'id': '126181383',
         'order': None,
         'type': None,
         'takerOrMaker': None,
         'side': 'sell',
         'price': 2.0,
         'amount': 30.0,
         'cost': 60.0,
         'fee': None}]
    )

    pair = 'ETH/USDT:USDT'
    res = await exchange._async_fetch_trades(pair, since=None, params=None)
    assert res[0][5] == 300


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange_name", EXCHANGES)
async def test__async_get_trade_history_id(default_conf, mocker, exchange_name,
                                           fetch_trades_result):

    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    pagination_arg = exchange._trades_pagination_arg

    async def mock_get_trade_hist(pair, *args, **kwargs):
        if 'since' in kwargs:
            # Return first 3
            return fetch_trades_result[:-2]
        elif kwargs.get('params', {}).get(pagination_arg) == fetch_trades_result[-3]['id']:
            # Return 2
            return fetch_trades_result[-3:-1]
        else:
            # Return last 2
            return fetch_trades_result[-2:]
    # Monkey-patch async function
    exchange._api_async.fetch_trades = MagicMock(side_effect=mock_get_trade_hist)

    pair = 'ETH/BTC'
    ret = await exchange._async_get_trade_history_id(pair,
                                                     since=fetch_trades_result[0]['timestamp'],
                                                     until=fetch_trades_result[-1]['timestamp'] - 1)
    assert type(ret) is tuple
    assert ret[0] == pair
    assert type(ret[1]) is list
    assert len(ret[1]) == len(fetch_trades_result)
    assert exchange._api_async.fetch_trades.call_count == 3
    fetch_trades_cal = exchange._api_async.fetch_trades.call_args_list
    # first call (using since, not fromId)
    assert fetch_trades_cal[0][0][0] == pair
    assert fetch_trades_cal[0][1]['since'] == fetch_trades_result[0]['timestamp']

    # 2nd call
    assert fetch_trades_cal[1][0][0] == pair
    assert 'params' in fetch_trades_cal[1][1]
    assert exchange._ft_has['trades_pagination_arg'] in fetch_trades_cal[1][1]['params']


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange_name", EXCHANGES)
async def test__async_get_trade_history_time(default_conf, mocker, caplog, exchange_name,
                                             fetch_trades_result):

    caplog.set_level(logging.DEBUG)

    async def mock_get_trade_hist(pair, *args, **kwargs):
        if kwargs['since'] == fetch_trades_result[0]['timestamp']:
            return fetch_trades_result[:-1]
        else:
            return fetch_trades_result[-1:]

    caplog.set_level(logging.DEBUG)
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    # Monkey-patch async function
    exchange._api_async.fetch_trades = MagicMock(side_effect=mock_get_trade_hist)
    pair = 'ETH/BTC'
    ret = await exchange._async_get_trade_history_time(pair,
                                                       since=fetch_trades_result[0]['timestamp'],
                                                       until=fetch_trades_result[-1]['timestamp']-1)
    assert type(ret) is tuple
    assert ret[0] == pair
    assert type(ret[1]) is list
    assert len(ret[1]) == len(fetch_trades_result)
    assert exchange._api_async.fetch_trades.call_count == 2
    fetch_trades_cal = exchange._api_async.fetch_trades.call_args_list
    # first call (using since, not fromId)
    assert fetch_trades_cal[0][0][0] == pair
    assert fetch_trades_cal[0][1]['since'] == fetch_trades_result[0]['timestamp']

    # 2nd call
    assert fetch_trades_cal[1][0][0] == pair
    assert fetch_trades_cal[1][1]['since'] == fetch_trades_result[-2]['timestamp']
    assert log_has_re(r"Stopping because until was reached.*", caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange_name", EXCHANGES)
async def test__async_get_trade_history_time_empty(default_conf, mocker, caplog, exchange_name,
                                                   trades_history):

    caplog.set_level(logging.DEBUG)

    async def mock_get_trade_hist(pair, *args, **kwargs):
        if kwargs['since'] == trades_history[0][0]:
            return trades_history[:-1]
        else:
            return []

    caplog.set_level(logging.DEBUG)
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    # Monkey-patch async function
    exchange._async_fetch_trades = MagicMock(side_effect=mock_get_trade_hist)
    pair = 'ETH/BTC'
    ret = await exchange._async_get_trade_history_time(pair, since=trades_history[0][0],
                                                       until=trades_history[-1][0]-1)
    assert type(ret) is tuple
    assert ret[0] == pair
    assert type(ret[1]) is list
    assert len(ret[1]) == len(trades_history) - 1
    assert exchange._async_fetch_trades.call_count == 2
    fetch_trades_cal = exchange._async_fetch_trades.call_args_list
    # first call (using since, not fromId)
    assert fetch_trades_cal[0][0][0] == pair
    assert fetch_trades_cal[0][1]['since'] == trades_history[0][0]


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_get_historic_trades(default_conf, mocker, caplog, exchange_name, trades_history):
    mocker.patch('freqtrade.exchange.Exchange.exchange_has', return_value=True)
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)

    pair = 'ETH/BTC'

    exchange._async_get_trade_history_id = get_mock_coro((pair, trades_history))
    exchange._async_get_trade_history_time = get_mock_coro((pair, trades_history))
    ret = exchange.get_historic_trades(pair, since=trades_history[0][0],
                                       until=trades_history[-1][0])

    # Depending on the exchange, one or the other method should be called
    assert sum([exchange._async_get_trade_history_id.call_count,
                exchange._async_get_trade_history_time.call_count]) == 1

    assert len(ret) == 2
    assert ret[0] == pair
    assert len(ret[1]) == len(trades_history)


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_get_historic_trades_notsupported(default_conf, mocker, caplog, exchange_name,
                                          trades_history):
    mocker.patch('freqtrade.exchange.Exchange.exchange_has', return_value=False)
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)

    pair = 'ETH/BTC'

    with pytest.raises(OperationalException,
                       match="This exchange does not support downloading Trades."):
        exchange.get_historic_trades(pair, since=trades_history[0][0],
                                     until=trades_history[-1][0])


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_cancel_order_dry_run(default_conf, mocker, exchange_name):
    default_conf['dry_run'] = True
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    mocker.patch('freqtrade.exchange.Exchange._is_dry_limit_order_filled', return_value=True)
    assert exchange.cancel_order(order_id='123', pair='TKN/BTC') == {}
    assert exchange.cancel_stoploss_order(order_id='123', pair='TKN/BTC') == {}

    order = exchange.create_order('ETH/BTC', 'limit', "buy", 5, 0.55, 'gtc')

    cancel_order = exchange.cancel_order(order_id=order['id'], pair='ETH/BTC')
    assert order['id'] == cancel_order['id']
    assert order['amount'] == cancel_order['amount']
    assert order['symbol'] == cancel_order['symbol']
    assert cancel_order['status'] == 'canceled'


@pytest.mark.parametrize("exchange_name", EXCHANGES)
@pytest.mark.parametrize("order,result", [
    ({'status': 'closed', 'filled': 10}, False),
    ({'status': 'closed', 'filled': 0.0}, True),
    ({'status': 'canceled', 'filled': 0.0}, True),
    ({'status': 'canceled', 'filled': 10.0}, False),
    ({'status': 'unknown', 'filled': 10.0}, False),
    ({'result': 'testest123'}, False),
])
def test_check_order_canceled_empty(mocker, default_conf, exchange_name, order, result):
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    assert exchange.check_order_canceled_empty(order) == result


@pytest.mark.parametrize("exchange_name", EXCHANGES)
@pytest.mark.parametrize("order,result", [
    ({'status': 'closed', 'amount': 10, 'fee': {}}, True),
    ({'status': 'closed', 'amount': 0.0, 'fee': {}}, True),
    ({'status': 'canceled', 'amount': 0.0, 'fee': {}}, True),
    ({'status': 'canceled', 'amount': 10.0}, False),
    ({'amount': 10.0, 'fee': {}}, False),
    ({'result': 'testest123'}, False),
    ('hello_world', False),
])
def test_is_cancel_order_result_suitable(mocker, default_conf, exchange_name, order, result):
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    assert exchange.is_cancel_order_result_suitable(order) == result


@pytest.mark.parametrize("exchange_name", EXCHANGES)
@pytest.mark.parametrize("corder,call_corder,call_forder", [
    ({'status': 'closed', 'amount': 10, 'fee': {}}, 1, 0),
    ({'amount': 10, 'fee': {}}, 1, 1),
])
def test_cancel_order_with_result(default_conf, mocker, exchange_name, corder,
                                  call_corder, call_forder):
    default_conf['dry_run'] = False
    api_mock = MagicMock()
    api_mock.cancel_order = MagicMock(return_value=corder)
    api_mock.fetch_order = MagicMock(return_value={})
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    res = exchange.cancel_order_with_result('1234', 'ETH/BTC', 1234)
    assert isinstance(res, dict)
    assert api_mock.cancel_order.call_count == call_corder
    assert api_mock.fetch_order.call_count == call_forder


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_cancel_order_with_result_error(default_conf, mocker, exchange_name, caplog):
    default_conf['dry_run'] = False
    api_mock = MagicMock()
    api_mock.cancel_order = MagicMock(side_effect=ccxt.InvalidOrder("Did not find order"))
    api_mock.fetch_order = MagicMock(side_effect=ccxt.InvalidOrder("Did not find order"))
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)

    res = exchange.cancel_order_with_result('1234', 'ETH/BTC', 1541)
    assert isinstance(res, dict)
    assert log_has("Could not cancel order 1234 for ETH/BTC.", caplog)
    assert log_has("Could not fetch cancelled order 1234.", caplog)
    assert res['amount'] == 1541


# Ensure that if not dry_run, we should call API
@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_cancel_order(default_conf, mocker, exchange_name):
    default_conf['dry_run'] = False
    api_mock = MagicMock()
    api_mock.cancel_order = MagicMock(return_value={'id': '123'})
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    assert exchange.cancel_order(order_id='_', pair='TKN/BTC') == {'id': '123'}

    with pytest.raises(InvalidOrderException):
        api_mock.cancel_order = MagicMock(side_effect=ccxt.InvalidOrder("Did not find order"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.cancel_order(order_id='_', pair='TKN/BTC')
    assert api_mock.cancel_order.call_count == 1

    ccxt_exceptionhandlers(mocker, default_conf, api_mock, exchange_name,
                           "cancel_order", "cancel_order",
                           order_id='_', pair='TKN/BTC')


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_cancel_stoploss_order(default_conf, mocker, exchange_name):
    default_conf['dry_run'] = False
    api_mock = MagicMock()
    api_mock.cancel_order = MagicMock(return_value={'id': '123'})
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    assert exchange.cancel_stoploss_order(order_id='_', pair='TKN/BTC') == {'id': '123'}

    with pytest.raises(InvalidOrderException):
        api_mock.cancel_order = MagicMock(side_effect=ccxt.InvalidOrder("Did not find order"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.cancel_stoploss_order(order_id='_', pair='TKN/BTC')
    assert api_mock.cancel_order.call_count == 1

    ccxt_exceptionhandlers(mocker, default_conf, api_mock, exchange_name,
                           "cancel_stoploss_order", "cancel_order",
                           order_id='_', pair='TKN/BTC')


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_cancel_stoploss_order_with_result(default_conf, mocker, exchange_name):
    default_conf['dry_run'] = False
    mocker.patch('freqtrade.exchange.Exchange.fetch_stoploss_order', return_value={'for': 123})
    mocker.patch('freqtrade.exchange.Ftx.fetch_stoploss_order', return_value={'for': 123})
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)

    mocker.patch('freqtrade.exchange.Exchange.cancel_stoploss_order',
                 return_value={'fee': {}, 'status': 'canceled', 'amount': 1234})
    mocker.patch('freqtrade.exchange.Ftx.cancel_stoploss_order',
                 return_value={'fee': {}, 'status': 'canceled', 'amount': 1234})
    co = exchange.cancel_stoploss_order_with_result(order_id='_', pair='TKN/BTC', amount=555)
    assert co == {'fee': {}, 'status': 'canceled', 'amount': 1234}

    mocker.patch('freqtrade.exchange.Exchange.cancel_stoploss_order',
                 return_value='canceled')
    mocker.patch('freqtrade.exchange.Ftx.cancel_stoploss_order',
                 return_value='canceled')
    # Fall back to fetch_stoploss_order
    co = exchange.cancel_stoploss_order_with_result(order_id='_', pair='TKN/BTC', amount=555)
    assert co == {'for': 123}

    mocker.patch('freqtrade.exchange.Exchange.fetch_stoploss_order',
                 side_effect=InvalidOrderException(""))
    mocker.patch('freqtrade.exchange.Ftx.fetch_stoploss_order',
                 side_effect=InvalidOrderException(""))

    co = exchange.cancel_stoploss_order_with_result(order_id='_', pair='TKN/BTC', amount=555)
    assert co['amount'] == 555
    assert co == {'fee': {}, 'status': 'canceled', 'amount': 555, 'info': {}}

    with pytest.raises(InvalidOrderException):
        mocker.patch('freqtrade.exchange.Exchange.cancel_stoploss_order',
                     side_effect=InvalidOrderException("Did not find order"))
        mocker.patch('freqtrade.exchange.Ftx.cancel_stoploss_order',
                     side_effect=InvalidOrderException("Did not find order"))
        exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
        exchange.cancel_stoploss_order_with_result(order_id='_', pair='TKN/BTC', amount=123)


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_fetch_order(default_conf, mocker, exchange_name, caplog):
    default_conf['dry_run'] = True
    default_conf['exchange']['log_responses'] = True
    order = MagicMock()
    order.myid = 123
    order.symbol = 'TKN/BTC'

    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    exchange._dry_run_open_orders['X'] = order
    assert exchange.fetch_order('X', 'TKN/BTC').myid == 123

    with pytest.raises(InvalidOrderException, match=r'Tried to get an invalid dry-run-order.*'):
        exchange.fetch_order('Y', 'TKN/BTC')

    default_conf['dry_run'] = False
    api_mock = MagicMock()
    api_mock.fetch_order = MagicMock(return_value={'id': '123', 'amount': 2, 'symbol': 'TKN/BTC'})
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    assert exchange.fetch_order(
        'X', 'TKN/BTC') == {'id': '123', 'amount': 2, 'symbol': 'TKN/BTC'}
    assert log_has(
        ("API fetch_order: {\'id\': \'123\', \'amount\': 2, \'symbol\': \'TKN/BTC\'}"
         ),
        caplog
    )

    with pytest.raises(InvalidOrderException):
        api_mock.fetch_order = MagicMock(side_effect=ccxt.InvalidOrder("Order not found"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.fetch_order(order_id='_', pair='TKN/BTC')
    assert api_mock.fetch_order.call_count == 1

    api_mock.fetch_order = MagicMock(side_effect=ccxt.OrderNotFound("Order not found"))
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    with patch('freqtrade.exchange.common.time.sleep') as tm:
        with pytest.raises(InvalidOrderException):
            exchange.fetch_order(order_id='_', pair='TKN/BTC')
        # Ensure backoff is called
        assert tm.call_args_list[0][0][0] == 1
        assert tm.call_args_list[1][0][0] == 2
        if API_FETCH_ORDER_RETRY_COUNT > 2:
            assert tm.call_args_list[2][0][0] == 5
        if API_FETCH_ORDER_RETRY_COUNT > 3:
            assert tm.call_args_list[3][0][0] == 10
    assert api_mock.fetch_order.call_count == API_FETCH_ORDER_RETRY_COUNT + 1

    ccxt_exceptionhandlers(mocker, default_conf, api_mock, exchange_name,
                           'fetch_order', 'fetch_order', retries=API_FETCH_ORDER_RETRY_COUNT + 1,
                           order_id='_', pair='TKN/BTC')


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_fetch_stoploss_order(default_conf, mocker, exchange_name):
    # Don't test FTX here - that needs a separate test
    if exchange_name == 'ftx':
        return
    default_conf['dry_run'] = True
    order = MagicMock()
    order.myid = 123
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    exchange._dry_run_open_orders['X'] = order
    assert exchange.fetch_stoploss_order('X', 'TKN/BTC').myid == 123

    with pytest.raises(InvalidOrderException, match=r'Tried to get an invalid dry-run-order.*'):
        exchange.fetch_stoploss_order('Y', 'TKN/BTC')

    default_conf['dry_run'] = False
    api_mock = MagicMock()
    api_mock.fetch_order = MagicMock(return_value={'id': '123', 'symbol': 'TKN/BTC'})
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    assert exchange.fetch_stoploss_order('X', 'TKN/BTC') == {'id': '123', 'symbol': 'TKN/BTC'}

    with pytest.raises(InvalidOrderException):
        api_mock.fetch_order = MagicMock(side_effect=ccxt.InvalidOrder("Order not found"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
        exchange.fetch_stoploss_order(order_id='_', pair='TKN/BTC')
    assert api_mock.fetch_order.call_count == 1

    ccxt_exceptionhandlers(mocker, default_conf, api_mock, exchange_name,
                           'fetch_stoploss_order', 'fetch_order',
                           retries=API_FETCH_ORDER_RETRY_COUNT + 1,
                           order_id='_', pair='TKN/BTC')


def test_fetch_order_or_stoploss_order(default_conf, mocker):
    exchange = get_patched_exchange(mocker, default_conf, id='binance')
    fetch_order_mock = MagicMock()
    fetch_stoploss_order_mock = MagicMock()
    mocker.patch.multiple('freqtrade.exchange.Exchange',
                          fetch_order=fetch_order_mock,
                          fetch_stoploss_order=fetch_stoploss_order_mock,
                          )

    exchange.fetch_order_or_stoploss_order('1234', 'ETH/BTC', False)
    assert fetch_order_mock.call_count == 1
    assert fetch_order_mock.call_args_list[0][0][0] == '1234'
    assert fetch_order_mock.call_args_list[0][0][1] == 'ETH/BTC'
    assert fetch_stoploss_order_mock.call_count == 0

    fetch_order_mock.reset_mock()
    fetch_stoploss_order_mock.reset_mock()

    exchange.fetch_order_or_stoploss_order('1234', 'ETH/BTC', True)
    assert fetch_order_mock.call_count == 0
    assert fetch_stoploss_order_mock.call_count == 1
    assert fetch_stoploss_order_mock.call_args_list[0][0][0] == '1234'
    assert fetch_stoploss_order_mock.call_args_list[0][0][1] == 'ETH/BTC'


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_name(default_conf, mocker, exchange_name):
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)

    assert exchange.name == exchange_name.title()
    assert exchange.id == exchange_name


@pytest.mark.parametrize("trading_mode,amount", [
    ('spot', 0.2340606),
    ('futures', 2.340606),
])
@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_get_trades_for_order(default_conf, mocker, exchange_name, trading_mode, amount):
    order_id = 'ABCD-ABCD'
    since = datetime(2018, 5, 5, 0, 0, 0)
    default_conf["dry_run"] = False
    default_conf["trading_mode"] = trading_mode
    default_conf["collateral"] = 'isolated'
    mocker.patch('freqtrade.exchange.Exchange.exchange_has', return_value=True)
    api_mock = MagicMock()

    api_mock.fetch_my_trades = MagicMock(return_value=[{'id': 'TTR67E-3PFBD-76IISV',
                                                        'order': 'ABCD-ABCD',
                                                        'info': {'pair': 'XLTCZBTC',
                                                                 'time': 1519860024.4388,
                                                                 'type': 'buy',
                                                                 'ordertype': 'limit',
                                                                 'price': '20.00000',
                                                                 'cost': '38.62000',
                                                                 'fee': '0.06179',
                                                                 'vol': '5',
                                                                 'id': 'ABCD-ABCD'},
                                                        'timestamp': 1519860024438,
                                                        'datetime': '2018-02-28T23:20:24.438Z',
                                                        'symbol': 'ETH/USDT:USDT',
                                                        'type': 'limit',
                                                        'side': 'buy',
                                                        'price': 165.0,
                                                        'amount': 0.2340606,
                                                        'fee': {'cost': 0.06179, 'currency': 'BTC'}
                                                        }])

    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)

    orders = exchange.get_trades_for_order(order_id, 'ETH/USDT:USDT', since)
    assert len(orders) == 1
    assert orders[0]['price'] == 165
    assert isclose(orders[0]['amount'], amount)
    assert api_mock.fetch_my_trades.call_count == 1
    # since argument should be
    assert isinstance(api_mock.fetch_my_trades.call_args[0][1], int)
    assert api_mock.fetch_my_trades.call_args[0][0] == 'ETH/USDT:USDT'
    # Same test twice, hardcoded number and doing the same calculation
    assert api_mock.fetch_my_trades.call_args[0][1] == 1525478395000
    assert api_mock.fetch_my_trades.call_args[0][1] == int(since.replace(
        tzinfo=timezone.utc).timestamp() - 5) * 1000

    ccxt_exceptionhandlers(mocker, default_conf, api_mock, exchange_name,
                           'get_trades_for_order', 'fetch_my_trades',
                           order_id=order_id, pair='ETH/USDT:USDT', since=since)

    mocker.patch('freqtrade.exchange.Exchange.exchange_has', MagicMock(return_value=False))
    assert exchange.get_trades_for_order(order_id, 'ETH/USDT:USDT', since) == []


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_get_fee(default_conf, mocker, exchange_name):
    api_mock = MagicMock()
    api_mock.calculate_fee = MagicMock(return_value={
        'type': 'taker',
        'currency': 'BTC',
        'rate': 0.025,
        'cost': 0.05
    })
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    exchange._config.pop('fee', None)

    assert exchange.get_fee('ETH/BTC') == 0.025
    assert api_mock.calculate_fee.call_count == 1

    ccxt_exceptionhandlers(mocker, default_conf, api_mock, exchange_name,
                           'get_fee', 'calculate_fee', symbol="ETH/BTC")

    api_mock.calculate_fee.reset_mock()
    exchange._config['fee'] = 0.001

    assert exchange.get_fee('ETH/BTC') == 0.001
    assert api_mock.calculate_fee.call_count == 0


def test_stoploss_order_unsupported_exchange(default_conf, mocker):
    exchange = get_patched_exchange(mocker, default_conf, id='bittrex')
    with pytest.raises(OperationalException, match=r"stoploss is not implemented .*"):
        exchange.stoploss(
            pair='ETH/BTC',
            amount=1,
            stop_price=220,
            order_types={},
            side="sell",
            leverage=1.0
        )

    with pytest.raises(OperationalException, match=r"stoploss is not implemented .*"):
        exchange.stoploss_adjust(1, {}, side="sell")


def test_merge_ft_has_dict(default_conf, mocker):
    mocker.patch.multiple('freqtrade.exchange.Exchange',
                          _init_ccxt=MagicMock(return_value=MagicMock()),
                          _load_async_markets=MagicMock(),
                          validate_pairs=MagicMock(),
                          validate_timeframes=MagicMock(),
                          validate_stakecurrency=MagicMock()
                          )
    ex = Exchange(default_conf)
    assert ex._ft_has == Exchange._ft_has_default

    ex = Kraken(default_conf)
    assert ex._ft_has != Exchange._ft_has_default
    assert ex._ft_has['trades_pagination'] == 'id'
    assert ex._ft_has['trades_pagination_arg'] == 'since'

    # Binance defines different values
    ex = Binance(default_conf)
    assert ex._ft_has != Exchange._ft_has_default
    assert ex._ft_has['stoploss_on_exchange']
    assert ex._ft_has['order_time_in_force'] == ['gtc', 'fok', 'ioc']
    assert ex._ft_has['trades_pagination'] == 'id'
    assert ex._ft_has['trades_pagination_arg'] == 'fromId'

    conf = copy.deepcopy(default_conf)
    conf['exchange']['_ft_has_params'] = {"DeadBeef": 20,
                                          "stoploss_on_exchange": False}
    # Use settings from configuration (overriding stoploss_on_exchange)
    ex = Binance(conf)
    assert ex._ft_has != Exchange._ft_has_default
    assert not ex._ft_has['stoploss_on_exchange']
    assert ex._ft_has['DeadBeef'] == 20


def test_get_valid_pair_combination(default_conf, mocker, markets):
    mocker.patch.multiple('freqtrade.exchange.Exchange',
                          _init_ccxt=MagicMock(return_value=MagicMock()),
                          _load_async_markets=MagicMock(),
                          validate_pairs=MagicMock(),
                          validate_timeframes=MagicMock(),
                          markets=PropertyMock(return_value=markets))
    ex = Exchange(default_conf)

    assert ex.get_valid_pair_combination("ETH", "BTC") == "ETH/BTC"
    assert ex.get_valid_pair_combination("BTC", "ETH") == "ETH/BTC"
    with pytest.raises(DependencyException, match=r"Could not combine.* to get a valid pair."):
        ex.get_valid_pair_combination("NOPAIR", "ETH")


@pytest.mark.parametrize(
    "base_currencies,quote_currencies,tradable_only,active_only,spot_only,"
    "futures_only,expected_keys", [
        # Testing markets (in conftest.py):
        # 'BLK/BTC':  'active': True
        # 'BTT/BTC':  'active': True
        # 'ETH/BTC':  'active': True
        # 'ETH/USDT': 'active': True
        # 'LTC/BTC':  'active': False
        # 'LTC/ETH':  'active': True
        # 'LTC/USD':  'active': True
        # 'LTC/USDT': 'active': True
        # 'NEO/BTC':  'active': False
        # 'TKN/BTC':  'active'  not set
        # 'XLTCUSDT': 'active': True, not a pair
        # 'XRP/BTC':  'active': False
        # all markets
        ([], [], False, False, False, False,
         ['BLK/BTC', 'BTT/BTC', 'ETH/BTC', 'ETH/USDT', 'LTC/BTC', 'LTC/ETH', 'LTC/USD',
          'LTC/USDT', 'NEO/BTC', 'TKN/BTC', 'XLTCUSDT', 'XRP/BTC']),
        # all markets, only spot pairs
        ([], [], False, False, True, False,
         ['BLK/BTC', 'BTT/BTC', 'ETH/BTC', 'ETH/USDT', 'LTC/BTC', 'LTC/ETH', 'LTC/USD',
          'LTC/USDT', 'NEO/BTC', 'TKN/BTC', 'XRP/BTC']),
        # active markets
        ([], [], False, True, False, False,
         ['BLK/BTC', 'ETH/BTC', 'ETH/USDT', 'LTC/BTC', 'LTC/ETH', 'LTC/USD', 'NEO/BTC',
          'TKN/BTC', 'XLTCUSDT', 'XRP/BTC']),
        # all pairs
        ([], [], True, False, False, False,
         ['BLK/BTC', 'BTT/BTC', 'ETH/BTC', 'ETH/USDT', 'LTC/BTC', 'LTC/ETH', 'LTC/USD',
          'LTC/USDT', 'NEO/BTC', 'TKN/BTC', 'XRP/BTC']),
        # active pairs
        ([], [], True, True, False, False,
         ['BLK/BTC', 'ETH/BTC', 'ETH/USDT', 'LTC/BTC', 'LTC/ETH', 'LTC/USD', 'NEO/BTC',
          'TKN/BTC', 'XRP/BTC']),
        # all markets, base=ETH, LTC
        (['ETH', 'LTC'], [], False, False, False, False,
         ['ETH/BTC', 'ETH/USDT', 'LTC/BTC', 'LTC/ETH', 'LTC/USD', 'LTC/USDT', 'XLTCUSDT']),
        # all markets, base=LTC
        (['LTC'], [], False, False, False, False,
         ['LTC/BTC', 'LTC/ETH', 'LTC/USD', 'LTC/USDT', 'XLTCUSDT']),
        # spot markets, base=LTC
        (['LTC'], [], False, False, True, False,
         ['LTC/BTC', 'LTC/ETH', 'LTC/USD', 'LTC/USDT']),
        # all markets, quote=USDT
        ([], ['USDT'], False, False, False, False,
         ['ETH/USDT', 'LTC/USDT', 'XLTCUSDT']),
        # Futures markets, quote=USDT
        ([], ['USDT'], False, False, False, True,
         ['ETH/USDT', 'LTC/USDT']),
        # all markets, quote=USDT, USD
        ([], ['USDT', 'USD'], False, False, False, False,
         ['ETH/USDT', 'LTC/USD', 'LTC/USDT', 'XLTCUSDT']),
        # spot markets, quote=USDT, USD
        ([], ['USDT', 'USD'], False, False, True, False,
         ['ETH/USDT', 'LTC/USD', 'LTC/USDT']),
        # all markets, base=LTC, quote=USDT
        (['LTC'], ['USDT'], False, False, False, False,
         ['LTC/USDT', 'XLTCUSDT']),
        # all pairs, base=LTC, quote=USDT
        (['LTC'], ['USDT'], True, False, False, False,
         ['LTC/USDT']),
        # all markets, base=LTC, quote=USDT, NONEXISTENT
        (['LTC'], ['USDT', 'NONEXISTENT'], False, False, False, False,
         ['LTC/USDT', 'XLTCUSDT']),
        # all markets, base=LTC, quote=NONEXISTENT
        (['LTC'], ['NONEXISTENT'], False, False, False, False,
         []),
    ])
def test_get_markets(default_conf, mocker, markets_static,
                     base_currencies, quote_currencies, tradable_only, active_only,
                     spot_only, futures_only,
                     expected_keys):
    mocker.patch.multiple('freqtrade.exchange.Exchange',
                          _init_ccxt=MagicMock(return_value=MagicMock()),
                          _load_async_markets=MagicMock(),
                          validate_pairs=MagicMock(),
                          validate_timeframes=MagicMock(),
                          markets=PropertyMock(return_value=markets_static))
    ex = Exchange(default_conf)
    pairs = ex.get_markets(base_currencies,
                           quote_currencies,
                           tradable_only=tradable_only,
                           spot_only=spot_only,
                           futures_only=futures_only,
                           active_only=active_only)
    assert sorted(pairs.keys()) == sorted(expected_keys)


def test_get_markets_error(default_conf, mocker):
    ex = get_patched_exchange(mocker, default_conf)
    mocker.patch('freqtrade.exchange.Exchange.markets', PropertyMock(return_value=None))
    with pytest.raises(OperationalException, match="Markets were not loaded."):
        ex.get_markets('LTC', 'USDT', True, False)


@pytest.mark.parametrize("exchange_name", EXCHANGES)
def test_ohlcv_candle_limit(default_conf, mocker, exchange_name):
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    timeframes = ('1m', '5m', '1h')
    expected = exchange._ft_has['ohlcv_candle_limit']
    for timeframe in timeframes:
        if 'ohlcv_candle_limit_per_timeframe' in exchange._ft_has:
            expected = exchange._ft_has['ohlcv_candle_limit_per_timeframe'][timeframe]
            # This should only run for bittrex
            assert exchange_name == 'bittrex'
        assert exchange.ohlcv_candle_limit(timeframe) == expected


def test_timeframe_to_minutes():
    assert timeframe_to_minutes("5m") == 5
    assert timeframe_to_minutes("10m") == 10
    assert timeframe_to_minutes("1h") == 60
    assert timeframe_to_minutes("1d") == 1440


def test_timeframe_to_seconds():
    assert timeframe_to_seconds("5m") == 300
    assert timeframe_to_seconds("10m") == 600
    assert timeframe_to_seconds("1h") == 3600
    assert timeframe_to_seconds("1d") == 86400


def test_timeframe_to_msecs():
    assert timeframe_to_msecs("5m") == 300000
    assert timeframe_to_msecs("10m") == 600000
    assert timeframe_to_msecs("1h") == 3600000
    assert timeframe_to_msecs("1d") == 86400000


def test_timeframe_to_prev_date():
    # 2019-08-12 13:22:08
    date = datetime.fromtimestamp(1565616128, tz=timezone.utc)

    tf_list = [
        # 5m -> 2019-08-12 13:20:00
        ("5m", datetime(2019, 8, 12, 13, 20, 0, tzinfo=timezone.utc)),
        # 10m -> 2019-08-12 13:20:00
        ("10m", datetime(2019, 8, 12, 13, 20, 0, tzinfo=timezone.utc)),
        # 1h -> 2019-08-12 13:00:00
        ("1h", datetime(2019, 8, 12, 13, 00, 0, tzinfo=timezone.utc)),
        # 2h -> 2019-08-12 12:00:00
        ("2h", datetime(2019, 8, 12, 12, 00, 0, tzinfo=timezone.utc)),
        # 4h -> 2019-08-12 12:00:00
        ("4h", datetime(2019, 8, 12, 12, 00, 0, tzinfo=timezone.utc)),
        # 1d -> 2019-08-12 00:00:00
        ("1d", datetime(2019, 8, 12, 00, 00, 0, tzinfo=timezone.utc)),
    ]
    for interval, result in tf_list:
        assert timeframe_to_prev_date(interval, date) == result

    date = datetime.now(tz=timezone.utc)
    assert timeframe_to_prev_date("5m") < date
    # Does not round
    time = datetime(2019, 8, 12, 13, 20, 0, tzinfo=timezone.utc)
    assert timeframe_to_prev_date('5m', time) == time
    time = datetime(2019, 8, 12, 13, 0, 0, tzinfo=timezone.utc)
    assert timeframe_to_prev_date('1h', time) == time


def test_timeframe_to_next_date():
    # 2019-08-12 13:22:08
    date = datetime.fromtimestamp(1565616128, tz=timezone.utc)
    tf_list = [
        # 5m -> 2019-08-12 13:25:00
        ("5m", datetime(2019, 8, 12, 13, 25, 0, tzinfo=timezone.utc)),
        # 10m -> 2019-08-12 13:30:00
        ("10m", datetime(2019, 8, 12, 13, 30, 0, tzinfo=timezone.utc)),
        # 1h -> 2019-08-12 14:00:00
        ("1h", datetime(2019, 8, 12, 14, 00, 0, tzinfo=timezone.utc)),
        # 2h -> 2019-08-12 14:00:00
        ("2h", datetime(2019, 8, 12, 14, 00, 0, tzinfo=timezone.utc)),
        # 4h -> 2019-08-12 14:00:00
        ("4h", datetime(2019, 8, 12, 16, 00, 0, tzinfo=timezone.utc)),
        # 1d -> 2019-08-13 00:00:00
        ("1d", datetime(2019, 8, 13, 0, 0, 0, tzinfo=timezone.utc)),
    ]

    for interval, result in tf_list:
        assert timeframe_to_next_date(interval, date) == result

    date = datetime.now(tz=timezone.utc)
    assert timeframe_to_next_date("5m") > date

    date = datetime(2019, 8, 12, 13, 30, 0, tzinfo=timezone.utc)
    assert timeframe_to_next_date("5m", date) == date + timedelta(minutes=5)


@pytest.mark.parametrize(
    "market_symbol,base,quote,exchange,spot,margin,futures,trademode,add_dict,expected_result",
    [
        ("BTC/USDT", 'BTC', 'USDT', "binance", True, False, False, 'spot', {}, True),
        ("USDT/BTC", 'USDT', 'BTC', "binance", True, False, False, 'spot', {}, True),
        # No seperating /
        ("BTCUSDT", 'BTC', 'USDT', "binance", True, False, False, 'spot', {}, True),
        ("BTCUSDT", None, "USDT", "binance", True, False, False, 'spot', {}, False),
        ("USDT/BTC", "BTC", None, "binance", True, False, False, 'spot', {}, False),
        ("BTCUSDT", "BTC", None, "binance", True, False, False, 'spot', {}, False),
        ("BTC/USDT", "BTC", "USDT", "binance", True, False, False, 'spot', {}, True),
        # Futures mode, spot pair
        ("BTC/USDT", "BTC", "USDT", "binance", True, False, False, 'futures', {}, False),
        ("BTC/USDT", "BTC", "USDT", "binance", True, False, False, 'margin', {}, False),
        ("BTC/USDT", "BTC", "USDT", "binance", True, True, True, 'margin', {}, True),
        ("BTC/USDT", "BTC", "USDT", "binance", False, True, False, 'margin', {}, True),
        # Futures mode, futures pair
        ("BTC/USDT", "BTC", "USDT", "binance", False, False, True, 'futures', {}, True),
        # Futures market
        ("BTC/UNK", "BTC", 'UNK', "binance", False, False, True, 'spot', {}, False),
        ("BTC/EUR", 'BTC', 'EUR', "kraken", True, False, False, 'spot', {"darkpool": False}, True),
        ("EUR/BTC", 'EUR', 'BTC', "kraken", True, False, False, 'spot', {"darkpool": False}, True),
        # no darkpools
        ("BTC/EUR", 'BTC', 'EUR', "kraken", True, False, False, 'spot',
         {"darkpool": True}, False),
        # no darkpools
        ("BTC/EUR.d", 'BTC', 'EUR', "kraken", True, False, False, 'spot',
         {"darkpool": True}, False),
        ("BTC/USD", 'BTC', 'USD', "ftx", True, False, False, 'spot', {}, True),
        ("USD/BTC", 'USD', 'BTC', "ftx", True, False, False, 'spot', {}, True),
        # Can only trade spot markets
        ("BTC/USD", 'BTC', 'USD', "ftx", False, False, True, 'spot', {}, False),
        ("BTC/USD", 'BTC', 'USD', "ftx", False, False, True, 'futures', {}, True),
        # Can only trade spot markets
        ("BTC-PERP", 'BTC', 'USD', "ftx", False, False, True, 'spot', {}, False),
        ("BTC-PERP", 'BTC', 'USD', "ftx", False, False, True, 'margin', {}, False),
        ("BTC-PERP", 'BTC', 'USD', "ftx", False, False, True, 'futures', {}, True),

        ("BTC/USDT:USDT", 'BTC', 'USD', "okex", False, False, True, 'spot', {}, False),
        ("BTC/USDT:USDT", 'BTC', 'USD', "okex", False, False, True, 'margin', {}, False),
        ("BTC/USDT:USDT", 'BTC', 'USD', "okex", False, False, True, 'futures', {}, True),
    ])
def test_market_is_tradable(
        mocker, default_conf, market_symbol, base,
        quote, spot, margin, futures, trademode, add_dict, exchange, expected_result
) -> None:
    default_conf['trading_mode'] = trademode
    mocker.patch('freqtrade.exchange.exchange.Exchange.validate_trading_mode_and_collateral')
    ex = get_patched_exchange(mocker, default_conf, id=exchange)
    market = {
        'symbol': market_symbol,
        'base': base,
        'quote': quote,
        'spot': spot,
        'future': futures,
        'swap': futures,
        'margin': margin,
        **(add_dict),
    }
    assert ex.market_is_tradable(market) == expected_result


@pytest.mark.parametrize("market,expected_result", [
    ({'symbol': 'ETH/BTC', 'active': True}, True),
    ({'symbol': 'ETH/BTC', 'active': False}, False),
    ({'symbol': 'ETH/BTC', }, True),
])
def test_market_is_active(market, expected_result) -> None:
    assert market_is_active(market) == expected_result


@pytest.mark.parametrize("order,expected", [
    ([{'fee'}], False),
    ({'fee': None}, False),
    ({'fee': {'currency': 'ETH/BTC'}}, False),
    ({'fee': {'currency': 'ETH/BTC', 'cost': None}}, False),
    ({'fee': {'currency': 'ETH/BTC', 'cost': 0.01}}, True),
])
def test_order_has_fee(order, expected) -> None:
    assert Exchange.order_has_fee(order) == expected


@pytest.mark.parametrize("order,expected", [
    ({'symbol': 'ETH/BTC', 'fee': {'currency': 'ETH', 'cost': 0.43}},
        (0.43, 'ETH', 0.01)),
    ({'symbol': 'ETH/USDT', 'fee': {'currency': 'USDT', 'cost': 0.01}},
        (0.01, 'USDT', 0.01)),
    ({'symbol': 'BTC/USDT', 'fee': {'currency': 'USDT', 'cost': 0.34, 'rate': 0.01}},
        (0.34, 'USDT', 0.01)),
])
def test_extract_cost_curr_rate(mocker, default_conf, order, expected) -> None:
    mocker.patch('freqtrade.exchange.Exchange.calculate_fee_rate', MagicMock(return_value=0.01))
    ex = get_patched_exchange(mocker, default_conf)
    assert ex.extract_cost_curr_rate(order) == expected


@pytest.mark.parametrize("order,unknown_fee_rate,expected", [
    # Using base-currency
    ({'symbol': 'ETH/BTC', 'amount': 0.04, 'cost': 0.05,
        'fee': {'currency': 'ETH', 'cost': 0.004, 'rate': None}}, None, 0.1),
    ({'symbol': 'ETH/BTC', 'amount': 0.05, 'cost': 0.05,
        'fee': {'currency': 'ETH', 'cost': 0.004, 'rate': None}}, None, 0.08),
    # Using quote currency
    ({'symbol': 'ETH/BTC', 'amount': 0.04, 'cost': 0.05,
        'fee': {'currency': 'BTC', 'cost': 0.005}}, None, 0.1),
    ({'symbol': 'ETH/BTC', 'amount': 0.04, 'cost': 0.05,
        'fee': {'currency': 'BTC', 'cost': 0.002, 'rate': None}}, None, 0.04),
    # Using foreign currency
    ({'symbol': 'ETH/BTC', 'amount': 0.04, 'cost': 0.05,
        'fee': {'currency': 'NEO', 'cost': 0.0012}}, None, 0.001944),
    ({'symbol': 'ETH/BTC', 'amount': 2.21, 'cost': 0.02992561,
        'fee': {'currency': 'NEO', 'cost': 0.00027452}}, None, 0.00074305),
    # Rate included in return - return as is
    ({'symbol': 'ETH/BTC', 'amount': 0.04, 'cost': 0.05,
        'fee': {'currency': 'USDT', 'cost': 0.34, 'rate': 0.01}}, None, 0.01),
    ({'symbol': 'ETH/BTC', 'amount': 0.04, 'cost': 0.05,
        'fee': {'currency': 'USDT', 'cost': 0.34, 'rate': 0.005}}, None, 0.005),
    # 0.1% filled - no costs (kraken - #3431)
    ({'symbol': 'ETH/BTC', 'amount': 0.04, 'cost': 0.0,
      'fee': {'currency': 'BTC', 'cost': 0.0, 'rate': None}}, None, None),
    ({'symbol': 'ETH/BTC', 'amount': 0.04, 'cost': 0.0,
      'fee': {'currency': 'ETH', 'cost': 0.0, 'rate': None}}, None, 0.0),
    ({'symbol': 'ETH/BTC', 'amount': 0.04, 'cost': 0.0,
      'fee': {'currency': 'NEO', 'cost': 0.0, 'rate': None}}, None, None),
    # Invalid pair combination - POINT/BTC is not a pair
    ({'symbol': 'POINT/BTC', 'amount': 0.04, 'cost': 0.5,
      'fee': {'currency': 'POINT', 'cost': 2.0, 'rate': None}}, None, None),
    ({'symbol': 'POINT/BTC', 'amount': 0.04, 'cost': 0.5,
      'fee': {'currency': 'POINT', 'cost': 2.0, 'rate': None}}, 1, 4.0),
    ({'symbol': 'POINT/BTC', 'amount': 0.04, 'cost': 0.5,
      'fee': {'currency': 'POINT', 'cost': 2.0, 'rate': None}}, 2, 8.0),
])
def test_calculate_fee_rate(mocker, default_conf, order, expected, unknown_fee_rate) -> None:
    mocker.patch('freqtrade.exchange.Exchange.fetch_ticker', return_value={'last': 0.081})
    if unknown_fee_rate:
        default_conf['exchange']['unknown_fee_rate'] = unknown_fee_rate

    ex = get_patched_exchange(mocker, default_conf)

    assert ex.calculate_fee_rate(order) == expected


@pytest.mark.parametrize('retrycount,max_retries,expected', [
    (0, 3, 10),
    (1, 3, 5),
    (2, 3, 2),
    (3, 3, 1),
    (0, 1, 2),
    (1, 1, 1),
    (0, 4, 17),
    (1, 4, 10),
    (2, 4, 5),
    (3, 4, 2),
    (4, 4, 1),
    (0, 5, 26),
    (1, 5, 17),
    (2, 5, 10),
    (3, 5, 5),
    (4, 5, 2),
    (5, 5, 1),
])
def test_calculate_backoff(retrycount, max_retries, expected):
    assert calculate_backoff(retrycount, max_retries) == expected


@pytest.mark.parametrize("exchange_name", ['binance', 'ftx'])
def test__get_funding_fees_from_exchange(default_conf, mocker, exchange_name):
    api_mock = MagicMock()
    api_mock.fetch_funding_history = MagicMock(return_value=[
        {
            'amount': 0.14542,
            'code': 'USDT',
            'datetime': '2021-09-01T08:00:01.000Z',
            'id': '485478',
            'info': {'asset': 'USDT',
                     'income': '0.14542',
                     'incomeType': 'FUNDING_FEE',
                     'info': 'FUNDING_FEE',
                     'symbol': 'XRPUSDT',
                     'time': '1630382001000',
                     'tradeId': '',
                     'tranId': '993203'},
            'symbol': 'XRP/USDT',
            'timestamp': 1630382001000
        },
        {
            'amount': -0.14642,
            'code': 'USDT',
            'datetime': '2021-09-01T16:00:01.000Z',
            'id': '485479',
            'info': {'asset': 'USDT',
                     'income': '-0.14642',
                     'incomeType': 'FUNDING_FEE',
                     'info': 'FUNDING_FEE',
                     'symbol': 'XRPUSDT',
                     'time': '1630314001000',
                     'tradeId': '',
                     'tranId': '993204'},
            'symbol': 'XRP/USDT',
            'timestamp': 1630314001000
        }
    ])
    type(api_mock).has = PropertyMock(return_value={'fetchFundingHistory': True})

    # mocker.patch('freqtrade.exchange.Exchange.get_funding_fees', lambda pair, since: y)
    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange_name)
    date_time = datetime.strptime("2021-09-01T00:00:01.000Z", '%Y-%m-%dT%H:%M:%S.%fZ')
    unix_time = int(date_time.timestamp())
    expected_fees = -0.001  # 0.14542341 + -0.14642341
    fees_from_datetime = exchange._get_funding_fees_from_exchange(
        pair='XRP/USDT',
        since=date_time
    )
    fees_from_unix_time = exchange._get_funding_fees_from_exchange(
        pair='XRP/USDT',
        since=unix_time
    )

    assert(isclose(expected_fees, fees_from_datetime))
    assert(isclose(expected_fees, fees_from_unix_time))

    ccxt_exceptionhandlers(
        mocker,
        default_conf,
        api_mock,
        exchange_name,
        "_get_funding_fees_from_exchange",
        "fetch_funding_history",
        pair="XRP/USDT",
        since=unix_time
    )


@pytest.mark.parametrize('exchange', ['binance', 'kraken', 'ftx'])
@pytest.mark.parametrize('stake_amount,leverage,min_stake_with_lev', [
    (9.0, 3.0, 3.0),
    (20.0, 5.0, 4.0),
    (100.0, 100.0, 1.0)
])
def test_get_stake_amount_considering_leverage(
    exchange,
    stake_amount,
    leverage,
    min_stake_with_lev,
    mocker,
    default_conf
):
    exchange = get_patched_exchange(mocker, default_conf, id=exchange)
    assert exchange._get_stake_amount_considering_leverage(
        stake_amount, leverage) == min_stake_with_lev


@pytest.mark.parametrize("exchange_name,trading_mode", [
    ("binance", TradingMode.FUTURES),
    ("ftx", TradingMode.MARGIN),
    ("ftx", TradingMode.FUTURES)
])
def test__set_leverage(mocker, default_conf, exchange_name, trading_mode):

    api_mock = MagicMock()
    api_mock.set_leverage = MagicMock()
    type(api_mock).has = PropertyMock(return_value={'setLeverage': True})
    default_conf['dry_run'] = False

    ccxt_exceptionhandlers(
        mocker,
        default_conf,
        api_mock,
        exchange_name,
        "_set_leverage",
        "set_leverage",
        pair="XRP/USDT",
        leverage=5.0,
        trading_mode=trading_mode
    )


@pytest.mark.parametrize("collateral", [
    (Collateral.CROSS),
    (Collateral.ISOLATED)
])
def test_set_margin_mode(mocker, default_conf, collateral):

    api_mock = MagicMock()
    api_mock.set_margin_mode = MagicMock()
    type(api_mock).has = PropertyMock(return_value={'setMarginMode': True})
    default_conf['dry_run'] = False

    ccxt_exceptionhandlers(
        mocker,
        default_conf,
        api_mock,
        "binance",
        "set_margin_mode",
        "set_margin_mode",
        pair="XRP/USDT",
        collateral=collateral
    )


@pytest.mark.parametrize("exchange_name, trading_mode, collateral, exception_thrown", [
    ("binance", TradingMode.SPOT, None, False),
    ("binance", TradingMode.MARGIN, Collateral.ISOLATED, True),
    ("kraken", TradingMode.SPOT, None, False),
    ("kraken", TradingMode.MARGIN, Collateral.ISOLATED, True),
    ("kraken", TradingMode.FUTURES, Collateral.ISOLATED, True),
    ("ftx", TradingMode.SPOT, None, False),
    ("ftx", TradingMode.MARGIN, Collateral.ISOLATED, True),
    ("ftx", TradingMode.FUTURES, Collateral.ISOLATED, True),
    ("bittrex", TradingMode.SPOT, None, False),
    ("bittrex", TradingMode.MARGIN, Collateral.CROSS, True),
    ("bittrex", TradingMode.MARGIN, Collateral.ISOLATED, True),
    ("bittrex", TradingMode.FUTURES, Collateral.CROSS, True),
    ("bittrex", TradingMode.FUTURES, Collateral.ISOLATED, True),
    ("gateio", TradingMode.MARGIN, Collateral.ISOLATED, True),
    ("okex", TradingMode.SPOT, None, False),
    ("okex", TradingMode.MARGIN, Collateral.CROSS, True),
    ("okex", TradingMode.MARGIN, Collateral.ISOLATED, True),
    ("okex", TradingMode.FUTURES, Collateral.CROSS, True),

    ("binance", TradingMode.FUTURES, Collateral.ISOLATED, False),
    ("gateio", TradingMode.FUTURES, Collateral.ISOLATED, False),
    ("okex", TradingMode.FUTURES, Collateral.ISOLATED, False),

    # * Remove once implemented
    ("binance", TradingMode.MARGIN, Collateral.CROSS, True),
    ("binance", TradingMode.FUTURES, Collateral.CROSS, True),
    ("kraken", TradingMode.MARGIN, Collateral.CROSS, True),
    ("kraken", TradingMode.FUTURES, Collateral.CROSS, True),
    ("ftx", TradingMode.MARGIN, Collateral.CROSS, True),
    ("ftx", TradingMode.FUTURES, Collateral.CROSS, True),
    ("gateio", TradingMode.MARGIN, Collateral.CROSS, True),
    ("gateio", TradingMode.FUTURES, Collateral.CROSS, True),

    # * Uncomment once implemented
    # ("binance", TradingMode.MARGIN, Collateral.CROSS, False),
    # ("binance", TradingMode.FUTURES, Collateral.CROSS, False),
    # ("kraken", TradingMode.MARGIN, Collateral.CROSS, False),
    # ("kraken", TradingMode.FUTURES, Collateral.CROSS, False),
    # ("ftx", TradingMode.MARGIN, Collateral.CROSS, False),
    # ("ftx", TradingMode.FUTURES, Collateral.CROSS, False),
    # ("gateio", TradingMode.MARGIN, Collateral.CROSS, False),
    # ("gateio", TradingMode.FUTURES, Collateral.CROSS, False),
])
def test_validate_trading_mode_and_collateral(
    default_conf,
    mocker,
    exchange_name,
    trading_mode,
    collateral,
    exception_thrown
):
    exchange = get_patched_exchange(
        mocker, default_conf, id=exchange_name, mock_supported_modes=False)
    if (exception_thrown):
        with pytest.raises(OperationalException):
            exchange.validate_trading_mode_and_collateral(trading_mode, collateral)
    else:
        exchange.validate_trading_mode_and_collateral(trading_mode, collateral)


@pytest.mark.parametrize("exchange_name,trading_mode,ccxt_config", [
    ("binance", "spot", {}),
    ("binance", "margin", {"options": {"defaultType": "margin"}}),
    ("binance", "futures", {"options": {"defaultType": "future"}}),
    ("bibox", "spot", {"has": {"fetchCurrencies": False}}),
    ("bibox", "margin", {"has": {"fetchCurrencies": False}, "options": {"defaultType": "margin"}}),
    ("bibox", "futures", {"has": {"fetchCurrencies": False}, "options": {"defaultType": "swap"}}),
    ("bybit", "futures", {"options": {"defaultType": "linear"}}),
    ("ftx", "futures", {"options": {"defaultType": "swap"}}),
    ("gateio", "futures", {"options": {"defaultType": "swap"}}),
    ("hitbtc", "futures", {"options": {"defaultType": "swap"}}),
    ("kraken", "futures", {"options": {"defaultType": "swap"}}),
    ("kucoin", "futures", {"options": {"defaultType": "swap"}}),
    ("okex", "futures", {"options": {"defaultType": "swap"}}),
])
def test__ccxt_config(
    default_conf,
    mocker,
    exchange_name,
    trading_mode,
    ccxt_config
):
    default_conf['trading_mode'] = trading_mode
    default_conf['collateral'] = 'isolated'
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    assert exchange._ccxt_config == ccxt_config


@pytest.mark.parametrize('pair,nominal_value,max_lev', [
    ("ETH/BTC", 0.0, 2.0),
    ("TKN/BTC", 100.0, 5.0),
    ("BLK/BTC", 173.31, 3.0),
    ("LTC/BTC", 0.0, 1.0),
    ("TKN/USDT", 210.30, 1.0),
])
def test_get_max_leverage(default_conf, mocker, pair, nominal_value, max_lev):
    # Binance has a different method of getting the max leverage
    exchange = get_patched_exchange(mocker, default_conf, id="kraken")
    assert exchange.get_max_leverage(pair, nominal_value) == max_lev


@pytest.mark.parametrize(
    'size,funding_rate,mark_price,time_in_ratio,funding_fee,kraken_fee', [
        (10, 0.0001, 2.0, 1.0, 0.002, 0.002),
        (10, 0.0002, 2.0, 0.01, 0.004, 0.00004),
        (10, 0.0002, 2.5, None, 0.005, None),
    ])
def test_calculate_funding_fees(
    default_conf,
    mocker,
    size,
    funding_rate,
    mark_price,
    funding_fee,
    kraken_fee,
    time_in_ratio
):
    exchange = get_patched_exchange(mocker, default_conf)
    kraken = get_patched_exchange(mocker, default_conf, id="kraken")
    prior_date = timeframe_to_prev_date('1h', datetime.now(timezone.utc) - timedelta(hours=1))
    trade_date = timeframe_to_prev_date('1h', datetime.now(timezone.utc))
    funding_rates = DataFrame([
        {'date': prior_date, 'open': funding_rate},  # Line not used.
        {'date': trade_date, 'open': funding_rate},
    ])
    mark_rates = DataFrame([
        {'date': prior_date, 'open': mark_price},
        {'date': trade_date, 'open': mark_price},
    ])
    df = exchange.combine_funding_and_mark(funding_rates, mark_rates)

    assert exchange.calculate_funding_fees(
        df,
        amount=size,
        is_short=True,
        open_date=trade_date,
        close_date=trade_date,
        time_in_ratio=time_in_ratio,
    ) == funding_fee

    if (kraken_fee is None):
        with pytest.raises(OperationalException):
            kraken.calculate_funding_fees(
                df,
                amount=size,
                is_short=True,
                open_date=trade_date,
                close_date=trade_date,
                time_in_ratio=time_in_ratio,
            )

    else:
        assert kraken.calculate_funding_fees(
            df,
            amount=size,
            is_short=True,
            open_date=trade_date,
            close_date=trade_date,
            time_in_ratio=time_in_ratio,
        ) == kraken_fee


def test_get_liquidation_price(mocker, default_conf):

    api_mock = MagicMock()
    positions = [
        {
            'info': {},
            'symbol': 'NEAR/USDT:USDT',
            'timestamp': 1642164737148,
            'datetime': '2022-01-14T12:52:17.148Z',
            'initialMargin': 1.51072,
            'initialMarginPercentage': 0.1,
            'maintenanceMargin': 0.38916147,
            'maintenanceMarginPercentage': 0.025,
            'entryPrice': 18.884,
            'notional': 15.1072,
            'leverage': 9.97,
            'unrealizedPnl': 0.0048,
            'contracts': 8,
            'contractSize': 0.1,
            'marginRatio': None,
            'liquidationPrice': 17.47,
            'markPrice': 18.89,
            'collateral': 1.52549075,
            'marginType': 'isolated',
            'side': 'buy',
            'percentage': 0.003177292946409658
        }
    ]
    api_mock.fetch_positions = MagicMock(return_value=positions)
    mocker.patch.multiple(
        'freqtrade.exchange.Exchange',
        exchange_has=MagicMock(return_value=True),
    )
    default_conf['dry_run'] = False

    exchange = get_patched_exchange(mocker, default_conf, api_mock)
    liq_price = exchange.get_liquidation_price('NEAR/USDT:USDT')
    assert liq_price == 17.47

    ccxt_exceptionhandlers(
        mocker,
        default_conf,
        api_mock,
        "binance",
        "get_liquidation_price",
        "fetch_positions",
        pair="XRP/USDT"
    )


@pytest.mark.parametrize('exchange,rate_start,rate_end,d1,d2,amount,expected_fees', [
    ('binance', 0, 2, "2021-09-01 00:00:00", "2021-09-01 08:00:00",  30.0, -0.0009140999999999999),
    ('binance', 0, 2, "2021-09-01 00:00:15", "2021-09-01 08:00:00",  30.0, -0.0009140999999999999),
    ('binance', 1, 2, "2021-09-01 01:00:14", "2021-09-01 08:00:00",  30.0, -0.0002493),
    ('binance', 1, 2, "2021-09-01 00:00:16", "2021-09-01 08:00:00",  30.0, -0.0002493),
    ('binance', 0, 1, "2021-09-01 00:00:00", "2021-09-01 07:59:59",  30.0, -0.0006647999999999999),
    ('binance', 0, 2, "2021-09-01 00:00:00", "2021-09-01 12:00:00",  30.0, -0.0009140999999999999),
    ('binance', 0, 2, "2021-09-01 00:00:01", "2021-09-01 08:00:00",  30.0, -0.0009140999999999999),
    # TODO: Uncoment once _calculate_funding_fees can pas time_in_ratio to exchange._get_funding_fee
    # ('kraken', "2021-09-01 00:00:00", "2021-09-01 08:00:00",  30.0, -0.0014937),
    # ('kraken', "2021-09-01 00:00:15", "2021-09-01 08:00:00",  30.0, -0.0008289),
    # ('kraken', "2021-09-01 01:00:14", "2021-09-01 08:00:00",  30.0, -0.0008289),
    # ('kraken', "2021-09-01 00:00:00", "2021-09-01 07:59:59",  30.0, -0.0012443999999999999),
    # ('kraken', "2021-09-01 00:00:00", "2021-09-01 12:00:00", 30.0,  0.0045759),
    # ('kraken', "2021-09-01 00:00:01", "2021-09-01 08:00:00",  30.0, -0.0008289),
    ('ftx', 0, 9, "2021-09-01 00:00:00", "2021-09-01 08:00:00", 30.0,  0.0010008000000000003),
    ('ftx', 0, 13, "2021-09-01 00:00:00", "2021-09-01 12:00:00", 30.0,  0.0146691),
    ('ftx', 1, 9, "2021-09-01 00:00:01", "2021-09-01 08:00:00", 30.0,  0.0016656000000000002),
    ('gateio', 0, 2, "2021-09-01 00:00:00", "2021-09-01 08:00:00",  30.0, -0.0009140999999999999),
    ('gateio', 0, 2, "2021-09-01 00:00:00", "2021-09-01 12:00:00",  30.0, -0.0009140999999999999),
    ('gateio', 1, 2, "2021-09-01 00:00:01", "2021-09-01 08:00:00",  30.0, -0.0002493),
    ('binance', 0,  2, "2021-09-01 00:00:00", "2021-09-01 08:00:00",  50.0, -0.0015235000000000001),
    # TODO: Uncoment once _calculate_funding_fees can pas time_in_ratio to exchange._get_funding_fee
    # ('kraken', "2021-09-01 00:00:00", "2021-09-01 08:00:00",  50.0, -0.0024895),
    ('ftx', 0, 9, "2021-09-01 00:00:00", "2021-09-01 08:00:00", 50.0,  0.0016680000000000002),
])
def test__fetch_and_calculate_funding_fees(
    mocker,
    default_conf,
    funding_rate_history_hourly,
    funding_rate_history_octohourly,
    rate_start,
    rate_end,
    mark_ohlcv,
    exchange,
    d1,
    d2,
    amount,
    expected_fees
):
    """
    nominal_value = mark_price * size
    funding_fee = nominal_value * funding_rate
    size: 30
        time: 0, mark: 2.77, nominal_value: 83.1, fundRate: -0.000008, fundFee: -0.0006648
        time: 1, mark: 2.73, nominal_value: 81.9, fundRate: -0.000004, fundFee: -0.0003276
        time: 2, mark: 2.74, nominal_value: 82.2, fundRate: 0.000012, fundFee: 0.0009864
        time: 3, mark: 2.76, nominal_value: 82.8, fundRate: -0.000003, fundFee: -0.0002484
        time: 4, mark: 2.76, nominal_value: 82.8, fundRate: -0.000007, fundFee: -0.0005796
        time: 5, mark: 2.77, nominal_value: 83.1, fundRate: 0.000003, fundFee: 0.0002493
        time: 6, mark: 2.78, nominal_value: 83.4, fundRate: 0.000019, fundFee: 0.0015846
        time: 7, mark: 2.78, nominal_value: 83.4, fundRate: 0.000003, fundFee: 0.0002502
        time: 8, mark: 2.77, nominal_value: 83.1, fundRate: -0.000003, fundFee: -0.0002493
        time: 9, mark: 2.77, nominal_value: 83.1, fundRate: 0, fundFee: 0.0
        time: 10, mark: 2.84, nominal_value: 85.2, fundRate: 0.000013, fundFee: 0.0011076
        time: 11, mark: 2.81, nominal_value: 84.3, fundRate: 0.000077, fundFee: 0.0064911
        time: 12, mark: 2.81, nominal_value: 84.3, fundRate: 0.000072, fundFee: 0.0060696
        time: 13, mark: 2.82, nominal_value: 84.6, fundRate: 0.000097, fundFee: 0.0082062

    size: 50
        time: 0, mark: 2.77, nominal_value: 138.5, fundRate: -0.000008, fundFee: -0.001108
        time: 1, mark: 2.73, nominal_value: 136.5, fundRate: -0.000004, fundFee: -0.000546
        time: 2, mark: 2.74, nominal_value: 137.0, fundRate: 0.000012, fundFee: 0.001644
        time: 3, mark: 2.76, nominal_value: 138.0, fundRate: -0.000003, fundFee: -0.000414
        time: 4, mark: 2.76, nominal_value: 138.0, fundRate: -0.000007, fundFee: -0.000966
        time: 5, mark: 2.77, nominal_value: 138.5, fundRate: 0.000003, fundFee: 0.0004155
        time: 6, mark: 2.78, nominal_value: 139.0, fundRate: 0.000019, fundFee: 0.002641
        time: 7, mark: 2.78, nominal_value: 139.0, fundRate: 0.000003, fundFee: 0.000417
        time: 8, mark: 2.77, nominal_value: 138.5, fundRate: -0.000003, fundFee: -0.0004155
        time: 9, mark: 2.77, nominal_value: 138.5, fundRate: 0, fundFee: 0.0
        time: 10, mark: 2.84, nominal_value: 142.0, fundRate: 0.000013, fundFee: 0.001846
        time: 11, mark: 2.81, nominal_value: 140.5, fundRate: 0.000077, fundFee: 0.0108185
        time: 12, mark: 2.81, nominal_value: 140.5, fundRate: 0.000072, fundFee: 0.010116
        time: 13, mark: 2.82, nominal_value: 141.0, fundRate: 0.000097, fundFee: 0.013677
    """
    d1 = datetime.strptime(f"{d1} +0000", '%Y-%m-%d %H:%M:%S %z')
    d2 = datetime.strptime(f"{d2} +0000", '%Y-%m-%d %H:%M:%S %z')
    funding_rate_history = {
        'binance': funding_rate_history_octohourly,
        'ftx': funding_rate_history_hourly,
        'gateio': funding_rate_history_octohourly,
    }[exchange][rate_start:rate_end]
    api_mock = MagicMock()
    api_mock.fetch_funding_rate_history = get_mock_coro(return_value=funding_rate_history)
    api_mock.fetch_ohlcv = get_mock_coro(return_value=mark_ohlcv)
    type(api_mock).has = PropertyMock(return_value={'fetchOHLCV': True})
    type(api_mock).has = PropertyMock(return_value={'fetchFundingRateHistory': True})

    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange)
    funding_fees = exchange._fetch_and_calculate_funding_fees(
        pair='ADA/USDT', amount=amount, is_short=True, open_date=d1, close_date=d2)
    assert pytest.approx(funding_fees) == expected_fees
    # Fees for Longs are inverted
    funding_fees = exchange._fetch_and_calculate_funding_fees(
        pair='ADA/USDT', amount=amount, is_short=False, open_date=d1, close_date=d2)
    assert pytest.approx(funding_fees) == -expected_fees


@pytest.mark.parametrize('exchange,expected_fees', [
    ('binance', -0.0009140999999999999),
    ('gateio', -0.0009140999999999999),
])
def test__fetch_and_calculate_funding_fees_datetime_called(
    mocker,
    default_conf,
    funding_rate_history_octohourly,
    mark_ohlcv,
    exchange,
    time_machine,
    expected_fees
):
    api_mock = MagicMock()
    api_mock.fetch_ohlcv = get_mock_coro(return_value=mark_ohlcv)
    api_mock.fetch_funding_rate_history = get_mock_coro(
        return_value=funding_rate_history_octohourly)
    type(api_mock).has = PropertyMock(return_value={'fetchOHLCV': True})
    type(api_mock).has = PropertyMock(return_value={'fetchFundingRateHistory': True})

    exchange = get_patched_exchange(mocker, default_conf, api_mock, id=exchange)
    d1 = datetime.strptime("2021-09-01 00:00:00 +0000", '%Y-%m-%d %H:%M:%S %z')

    time_machine.move_to("2021-09-01 08:00:00 +00:00")
    # TODO-lev: test this for longs
    funding_fees = exchange._fetch_and_calculate_funding_fees('ADA/USDT', 30.0, True, d1)
    assert funding_fees == expected_fees


@pytest.mark.parametrize('pair,expected_size,trading_mode', [
    ('XLTCUSDT', 1, 'spot'),
    ('LTC/USD', 1, 'futures'),
    ('XLTCUSDT', 0.01, 'futures'),
    ('LTC/ETH', 1, 'futures'),
    ('ETH/USDT:USDT', 10, 'futures')
])
def test__get_contract_size(mocker, default_conf, pair, expected_size, trading_mode):
    api_mock = MagicMock()
    default_conf['trading_mode'] = trading_mode
    default_conf['collateral'] = 'isolated'
    mocker.patch('freqtrade.exchange.Exchange.markets', {
        'LTC/USD': {
            'symbol': 'LTC/USD',
            'contractSize': None,
        },
        'XLTCUSDT': {
            'symbol': 'XLTCUSDT',
            'contractSize': '0.01',
        },
        'LTC/ETH': {
            'symbol': 'LTC/ETH',
        },
        'ETH/USDT:USDT': {
            'symbol': 'ETH/USDT:USDT',
            'contractSize': '10',
        }
    })
    exchange = get_patched_exchange(mocker, default_conf, api_mock)
    size = exchange._get_contract_size(pair)
    assert expected_size == size


@pytest.mark.parametrize('pair,contract_size,trading_mode', [
    ('XLTCUSDT', 1, 'spot'),
    ('LTC/USD', 1, 'futures'),
    ('XLTCUSDT', 0.01, 'futures'),
    ('LTC/ETH', 1, 'futures'),
    ('ETH/USDT:USDT', 10, 'futures'),
])
def test__order_contracts_to_amount(
    mocker,
    default_conf,
    markets,
    pair,
    contract_size,
    trading_mode,
):
    api_mock = MagicMock()
    default_conf['trading_mode'] = trading_mode
    default_conf['collateral'] = 'isolated'
    mocker.patch('freqtrade.exchange.Exchange.markets', markets)
    exchange = get_patched_exchange(mocker, default_conf, api_mock)

    orders = [
        {
            'id': '123456320',
            'clientOrderId': '12345632018',
            'timestamp': 1640124992000,
            'datetime': 'Tue 21 Dec 2021 22:16:32 UTC',
            'lastTradeTimestamp': 1640124911000,
            'status': 'active',
            'symbol': pair,
            'type': 'limit',
            'timeInForce': 'gtc',
            'postOnly': None,
            'side': 'buy',
            'price': 2.0,
            'stopPrice': None,
            'average': None,
            'amount': 30.0,
            'cost': 60.0,
            'filled': None,
            'remaining': 30.0,
            'fee': 0.06,
            'fees': [{
                'currency': 'USDT',
                'cost': 0.06,
            }],
            'trades': None,
            'info': {},
        },
        {
            'id': '123456380',
            'clientOrderId': '12345638203',
            'timestamp': 1640124992000,
            'datetime': 'Tue 21 Dec 2021 22:16:32 UTC',
            'lastTradeTimestamp': 1640124911000,
            'status': 'active',
            'symbol': pair,
            'type': 'limit',
            'timeInForce': 'gtc',
            'postOnly': None,
            'side': 'sell',
            'price': 2.2,
            'stopPrice': None,
            'average': None,
            'amount': 40.0,
            'cost': 80.0,
            'filled': None,
            'remaining': 40.0,
            'fee': 0.08,
            'fees': [{
                'currency': 'USDT',
                'cost': 0.08,
            }],
            'trades': None,
            'info': {},
        },
    ]

    order1 = exchange._order_contracts_to_amount(orders[0])
    order2 = exchange._order_contracts_to_amount(orders[1])
    assert order1['amount'] == 30.0 * contract_size
    assert order2['amount'] == 40.0 * contract_size


@pytest.mark.parametrize('pair,contract_size,trading_mode', [
    ('XLTCUSDT', 1, 'spot'),
    ('LTC/USD', 1, 'futures'),
    ('XLTCUSDT', 0.01, 'futures'),
    ('LTC/ETH', 1, 'futures'),
    ('ETH/USDT:USDT', 10, 'futures'),
])
def test__trades_contracts_to_amount(
    mocker,
    default_conf,
    markets,
    pair,
    contract_size,
    trading_mode,
):
    api_mock = MagicMock()
    default_conf['trading_mode'] = trading_mode
    default_conf['collateral'] = 'isolated'
    mocker.patch('freqtrade.exchange.Exchange.markets', markets)
    exchange = get_patched_exchange(mocker, default_conf, api_mock)

    trades = [
        {
            'symbol': pair,
            'amount': 30.0,
        },
        {
            'symbol': pair,
            'amount': 40.0,
        }
    ]

    new_amount_trades = exchange._trades_contracts_to_amount(trades)
    assert new_amount_trades[0]['amount'] == 30.0 * contract_size
    assert new_amount_trades[1]['amount'] == 40.0 * contract_size


@pytest.mark.parametrize('pair,param_amount,param_size', [
    ('XLTCUSDT', 40, 4000),
    ('LTC/ETH', 30, 30),
    ('LTC/USD', 30, 30),
    ('ETH/USDT:USDT', 10, 1),
])
def test__amount_to_contracts(
    mocker,
    default_conf,
    markets,
    pair,
    param_amount,
    param_size
):
    api_mock = MagicMock()
    default_conf['trading_mode'] = 'spot'
    default_conf['collateral'] = 'isolated'
    mocker.patch('freqtrade.exchange.Exchange.markets', {
        'LTC/USD': {
            'symbol': 'LTC/USD',
            'contractSize': None,
        },
        'XLTCUSDT': {
            'symbol': 'XLTCUSDT',
            'contractSize': '0.01',
        },
        'LTC/ETH': {
            'symbol': 'LTC/ETH',
        },
        'ETH/USDT:USDT': {
            'symbol': 'ETH/USDT:USDT',
            'contractSize': '10',
        }
    })
    exchange = get_patched_exchange(mocker, default_conf, api_mock)
    result_size = exchange._amount_to_contracts(pair, param_amount)
    assert result_size == param_amount
    result_amount = exchange._contracts_to_amount(pair, param_size)
    assert result_amount == param_size

    default_conf['trading_mode'] = 'futures'
    exchange = get_patched_exchange(mocker, default_conf, api_mock)
    result_size = exchange._amount_to_contracts(pair, param_amount)
    assert result_size == param_size
    result_amount = exchange._contracts_to_amount(pair, param_size)
    assert result_amount == param_amount


@pytest.mark.parametrize('exchange_name,open_rate,is_short,leverage,trading_mode,collateral', [
    # Bittrex
    ('bittrex', "2.0", False, "3.0", spot, None),
    ('bittrex', "2.0", False, "1.0", spot, cross),
    ('bittrex', "2.0", True, "3.0", spot, isolated),
    # Binance
    ('binance', "2.0", False, "3.0", spot, None),
    ('binance', "2.0", False, "1.0", spot, cross),
    ('binance', "2.0", True, "3.0", spot, isolated),
])
def test_liquidation_price_is_none(
    mocker,
    default_conf,
    exchange_name,
    open_rate,
    is_short,
    leverage,
    trading_mode,
    collateral
):
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    assert exchange.liquidation_price(
        open_rate,
        is_short,
        leverage,
        trading_mode,
        collateral,
        1535443.01,
        71200.81144,
        -56354.57,
        135365.00,
        3683.979,
        0.10,
    ) is None


@pytest.mark.parametrize('exchange_name,open_rate,is_short,leverage,trading_mode,collateral', [
    # Bittrex
    ('bittrex', "2.0", False, "3.0", margin, cross),
    ('bittrex', "2.0", False, "3.0", margin, isolated),
    ('bittrex', "2.0", False, "3.0", futures, cross),
    ('bittrex', "2.0", False, "3.0", futures, isolated),
    # Binance
    # Binance supports isolated margin, but freqtrade likely won't for a while on Binance
    ('binance', "2.0", True, "3.0", margin, isolated),
    # Kraken
    ('kraken', "2.0", True, "1.0", margin, isolated),
    ('kraken', "2.0", True, "1.0", futures, isolated),
    # FTX
    ('ftx', "2.0", True, "3.0", margin, isolated),
    ('ftx', "2.0", True, "3.0", futures, isolated),
])
def test_liquidation_price_exception_thrown(
    exchange_name,
    open_rate,
    is_short,
    leverage,
    trading_mode,
    collateral,
    result
):
    # TODO-lev assert exception is thrown
    return  # Here to avoid indent error, remove when implemented


@pytest.mark.parametrize(
    'exchange_name, is_short, leverage, trading_mode, collateral, wallet_balance, '
    'mm_ex_1, upnl_ex_1, maintenance_amt, position, open_rate, '
    'mm_ratio, expected',
    [
        ("binance", False, 1, futures, isolated, 1535443.01, 0.0,
         0.0, 135365.00, 3683.979, 1456.84, 0.10, 1114.78),
        ("binance", False, 1, futures, isolated, 1535443.01, 0.0,
         0.0, 16300.000, 109.488, 32481.980, 0.025, 18778.73),
        ("binance", False, 1, futures, cross, 1535443.01, 71200.81144,
         -56354.57, 135365.00, 3683.979, 1456.84, 0.10, 1153.26),
        ("binance", False, 1, futures, cross, 1535443.01, 356512.508,
         -448192.89, 16300.000, 109.488, 32481.980, 0.025, 26316.89)
    ])
def test_liquidation_price(
    mocker, default_conf, exchange_name, open_rate, is_short, leverage, trading_mode,
    collateral, wallet_balance, mm_ex_1, upnl_ex_1, maintenance_amt, position, mm_ratio, expected
):
    exchange = get_patched_exchange(mocker, default_conf, id=exchange_name)
    assert isclose(round(exchange.liquidation_price(
        open_rate=open_rate,
        is_short=is_short,
        leverage=leverage,
        trading_mode=trading_mode,
        collateral=collateral,
        wallet_balance=wallet_balance,
        mm_ex_1=mm_ex_1,
        upnl_ex_1=upnl_ex_1,
        maintenance_amt=maintenance_amt,
        position=position,
        mm_ratio=mm_ratio
    ), 2), expected)
