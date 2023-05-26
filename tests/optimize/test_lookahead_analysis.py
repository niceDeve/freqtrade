# pragma pylint: disable=missing-docstring, W0212, line-too-long, C0103, unused-argument

from copy import deepcopy
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, PropertyMock

import pytest

from freqtrade.commands.optimize_commands import start_lookahead_analysis
from freqtrade.data.history import get_timerange
from freqtrade.exceptions import OperationalException
from freqtrade.optimize.lookahead_analysis import Analysis, LookaheadAnalysis
from freqtrade.optimize.lookahead_analysis_helpers import LookaheadAnalysisSubFunctions
from tests.conftest import EXMS, get_args, log_has_re, patch_exchange


@pytest.fixture
def lookahead_conf(default_conf_usdt):
    default_conf_usdt['minimum_trade_amount'] = 10
    default_conf_usdt['targeted_trade_amount'] = 20
    default_conf_usdt['strategy_path'] = str(
        Path(__file__).parent.parent / "strategy/strats/lookahead_bias")
    default_conf_usdt['strategy'] = 'strategy_test_v3_with_lookahead_bias'

    return default_conf_usdt


def test_start_lookahead_analysis(mocker):
    single_mock = MagicMock()
    text_table_mock = MagicMock()
    mocker.patch.multiple(
        'freqtrade.optimize.lookahead_analysis_helpers.LookaheadAnalysisSubFunctions',
        initialize_single_lookahead_analysis=single_mock,
        text_table_lookahead_analysis_instances=text_table_mock,
    )
    args = [
        "lookahead-analysis",
        "--strategy",
        "strategy_test_v3_with_lookahead_bias",
        "--strategy-path",
        str(Path(__file__).parent.parent / "strategy/strats/lookahead_bias"),
    ]
    pargs = get_args(args)
    pargs['config'] = None

    start_lookahead_analysis(pargs)
    assert single_mock.call_count == 1
    assert text_table_mock.call_count == 1

    single_mock.reset_mock()

    # Test invalid config
    args = [
        "lookahead-analysis",
        "--strategy",
        "strategy_test_v3_with_lookahead_bias",
        "--strategy-path",
        str(Path(__file__).parent.parent / "strategy/strats/lookahead_bias"),
        "--targeted-trade-amount",
        "10",
        "--minimum-trade-amount",
        "20",
    ]
    pargs = get_args(args)
    pargs['config'] = None
    with pytest.raises(OperationalException,
                       match=r"targeted trade amount can't be smaller than .*"):
        start_lookahead_analysis(pargs)


def test_lookahead_helper_invalid_config(lookahead_conf, mocker, caplog) -> None:
    conf = deepcopy(lookahead_conf)
    conf['targeted_trade_amount'] = 10
    conf['minimum_trade_amount'] = 40
    with pytest.raises(OperationalException,
                       match=r"targeted trade amount can't be smaller than .*"):
        LookaheadAnalysisSubFunctions.start(conf)

    conf = deepcopy(lookahead_conf)
    del conf['strategy']
    with pytest.raises(OperationalException,
                       match=r"No Strategy specified"):
        LookaheadAnalysisSubFunctions.start(conf)


def test_lookahead_helper_start(lookahead_conf, mocker, caplog) -> None:
    single_mock = MagicMock()
    text_table_mock = MagicMock()
    mocker.patch.multiple(
        'freqtrade.optimize.lookahead_analysis_helpers.LookaheadAnalysisSubFunctions',
        initialize_single_lookahead_analysis=single_mock,
        text_table_lookahead_analysis_instances=text_table_mock,
    )
    LookaheadAnalysisSubFunctions.start(lookahead_conf)
    assert single_mock.call_count == 1
    assert text_table_mock.call_count == 1

    single_mock.reset_mock()
    text_table_mock.reset_mock()


def test_lookahead_helper_text_table_lookahead_analysis_instances(lookahead_conf, caplog):
    analysis = Analysis()
    analysis.total_signals = 5
    analysis.has_bias = True
    analysis.false_entry_signals = 4
    analysis.false_exit_signals = 3

    strategy_obj = \
        {
            'name': "strategy_test_v3_with_lookahead_bias",
            'location': PurePosixPath(lookahead_conf['strategy_path'],
                                      f"{lookahead_conf['strategy']}.py")
        }

    instance = LookaheadAnalysis(lookahead_conf, strategy_obj)
    instance.current_analysis = analysis
    table, headers, data = (LookaheadAnalysisSubFunctions.
                            text_table_lookahead_analysis_instances([instance]))

    # check amount of returning rows
    assert len(data) == 1

    # check row contents for a try that errored out
    assert data[0][0] == 'strategy_test_v3_with_lookahead_bias.py'
    assert data[0][1] == 'strategy_test_v3_with_lookahead_bias'
    assert data[0][2].__contains__('error')
    assert len(data[0]) == 3

    # edit it into not showing an error
    instance.failed_bias_check = False
    table, headers, data = (LookaheadAnalysisSubFunctions.
                            text_table_lookahead_analysis_instances([instance]))
    assert data[0][0] == 'strategy_test_v3_with_lookahead_bias.py'
    assert data[0][1] == 'strategy_test_v3_with_lookahead_bias'
    assert data[0][2]  # True
    assert data[0][3] == 5
    assert data[0][4] == 4
    assert data[0][5] == 3
    assert data[0][6] == ''

    analysis.false_indicators.append('falseIndicator1')
    analysis.false_indicators.append('falseIndicator2')
    table, headers, data = (LookaheadAnalysisSubFunctions.
                            text_table_lookahead_analysis_instances([instance]))

    assert data[0][6] == 'falseIndicator1, falseIndicator2'


def test_lookahead_helper_export_to_csv():
    # TODO
    pytest.skip("TODO")


def test_initialize_single_lookahead_analysis():
    # TODO
    pytest.skip("TODO")


@pytest.mark.parametrize('scenario', [
    'no_bias', 'bias1'
])
def test_biased_strategy(lookahead_conf, mocker, caplog, scenario) -> None:
    mocker.patch('freqtrade.data.history.get_timerange', get_timerange)
    mocker.patch(f'{EXMS}.get_fee', return_value=0.0)
    mocker.patch(f'{EXMS}.get_min_pair_stake_amount', return_value=0.00001)
    mocker.patch(f'{EXMS}.get_max_pair_stake_amount', return_value=float('inf'))
    patch_exchange(mocker)
    mocker.patch('freqtrade.plugins.pairlistmanager.PairListManager.whitelist',
                 PropertyMock(return_value=['UNITTEST/BTC']))
    lookahead_conf['pairs'] = ['UNITTEST/USDT']

    lookahead_conf['timeframe'] = '5m'
    lookahead_conf['timerange'] = '20180119-20180122'

    # Patch scenario Parameter to allow for easy selection
    mocker.patch('freqtrade.strategy.hyper.HyperStrategyMixin.load_params_from_file',
                 return_value={
                     'params': {
                         "buy": {
                             "scenario": scenario
                         }
                     }
                 })

    strategy_obj = {}
    strategy_obj['name'] = "strategy_test_v3_with_lookahead_bias"
    instance = LookaheadAnalysis(lookahead_conf, strategy_obj)
    instance.start()
    # Assert init correct
    assert log_has_re(f"Strategy Parameter: scenario = {scenario}", caplog)
    # Assert bias detected
    assert log_has_re(r".*bias detected.*", caplog)
    # TODO: assert something ... most likely output (?) or instance state?

    # Assert False to see full logs in output
    # assert False
    # Run with `pytest tests/optimize/test_lookahead_analysis.py  -k test_biased_strategy`
