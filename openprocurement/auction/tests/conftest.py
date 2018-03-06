# -*- coding: utf-8 -*-
import logging
import os
from StringIO import StringIO

import pytest
import yaml


def pytest_generate_tests(metafunc):
    for funcargs in getattr(metafunc.function, 'funcarglist', ()):
        metafunc.addcall(funcargs=funcargs)


@pytest.fixture(scope='function')
def chronograph_config():
    chronograph_test_config = os.path.join(
        os.getcwd(),
        "openprocurement/auction/tests/data/auctions_chronograph.yaml"
    )
    with open(chronograph_test_config) as stream:
        config = yaml.load(stream)
    return config


class LogInterceptor(object):
    def __init__(self, logger, level):
        self.log_capture_string = StringIO()
        self.test_handler = logging.StreamHandler(self.log_capture_string)
        self.test_handler.setLevel(level)
        logger.addHandler(self.test_handler)


@pytest.fixture(scope='function')
def chronograph_logger():
    return LogInterceptor(logging.getLogger('Auction Chronograph'), 15)
