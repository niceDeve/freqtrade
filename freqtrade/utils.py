import logging
from argparse import Namespace
from typing import Any, Dict

from freqtrade.configuration import Configuration
from freqtrade.exchange import supported_exchanges
from freqtrade.state import RunMode


logger = logging.getLogger(__name__)


def setup_configuration(args: Namespace, method: RunMode) -> Dict[str, Any]:
    """
    Prepare the configuration for the Hyperopt module
    :param args: Cli args from Arguments()
    :return: Configuration
    """
    configuration = Configuration(args, method)
    config = configuration.load_config()

    # Ensure we do not use Exchange credentials
    config['exchange']['key'] = ''
    config['exchange']['secret'] = ''

    return config


def start_list_exchanges(args: Namespace) -> None:
    """
    Start listing known exchanges
    :param args: Cli args from Arguments()
    :return: None
    """

    # Initialize configuration
    config = setup_configuration(args, RunMode.OTHER)

    logger.debug('Starting freqtrade in cli-util mode')

    if args.print_one_column:
        print('\n'.join(supported_exchanges()))
    else:
        print(f"Supported exchanges: {', '.join(supported_exchanges())}")
