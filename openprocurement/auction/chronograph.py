from gevent import monkey
monkey.patch_all()

import os
import signal
from gevent import signal as gevent_signal
try:
    import urllib3.contrib.pyopenssl
    urllib3.contrib.pyopenssl.inject_into_urllib3()
except ImportError:
    pass

import logging
import logging.config
import argparse
from yaml import load
from zope.interface import implementer
from pytz import timezone
from gevent import sleep
from gevent.pywsgi import WSGIServer
from datetime import datetime, timedelta
from urlparse import urlparse

from openprocurement.auction.utils import FeedItem
from openprocurement.auction.core import components
from openprocurement.auction.interfaces import (
    IAuctionsChronograph, IAuctionsManager
)
from openprocurement.auction.design import sync_design_chronograph
from openprocurement.auction.helpers.chronograph import (
    get_server_name, AuctionScheduler, check_auction_workers
)
from openprocurement.auction.helpers.chronograph_http import chronograph_webapp
from openprocurement.auction.helpers.couch import (
    iterview, couchdb_dns_query_settings
)
from openprocurement.auction.helpers.system import get_lisener

logging.addLevelName(25, 'CHECK')


def check(self, msg, exc=None, *args, **kwargs):
    self.log(25, msg)
    if exc:
        self.error(exc, exc_info=True)


logging.Logger.check = check

LOGGER = logging.getLogger('Auction Chronograph')


@implementer(IAuctionsChronograph)
class AuctionsChronograph(object):

    def __init__(self, config, *args, **kwargs):
        super(AuctionsChronograph, self).__init__(*args, **kwargs)
        self.config = config
        self.timezone = timezone(config['main']['timezone'])
        self.mapper = components.qA(self, IAuctionsManager)
        self.server_name = get_server_name()
        LOGGER.info('Init node: {}'.format(self.server_name))
        self.init_services()

    def init_services(self):
        exceptions = []

        init_methods = [(self.init_database, 'CouchDB'),
                        (self.init_scheduler, 'Scheduler')]
        if self.config['main'].get('web_app', None):
            init_methods.append((self.init_web_app, 'WebApp'))

        # Checking Chronograph services
        for method, service in init_methods:
            result = ('ok', None)
            try:
                method()
            except Exception as e:
                exceptions.append(e)
                result = ('failed', e)
            LOGGER.check('{} - {}'.format(service, result[0]), result[1])

        # Checking Auction Workers
        worker_exceptions = check_auction_workers(self.config['main'])
        if worker_exceptions:
            exceptions.extend(worker_exceptions)
        if exceptions:
            raise exceptions[0]

    def init_database(self):

        database = couchdb_dns_query_settings(
            self.config['main']["couch_url"],
            self.config['main']['auctions_db']
        )
        sync_design_chronograph(database)

    def init_scheduler(self):
        self.scheduler = AuctionScheduler(
            self.server_name, self.config, logger=LOGGER,
            timezone=self.timezone
        )
        self.scheduler.chronograph = self
        self.scheduler.start()

    def init_web_app(self):
        self.web_application = chronograph_webapp
        self.web_application.chronograph = self
        location = self.config['main'].get('web_app')
        if ':' in str(location):
            if not location.startswith('//'):
                location = "//{}".format(location)
            o = urlparse(location)
            self.server = WSGIServer(
                get_lisener(o.port, o.hostname),
                self.web_application,
                spawn=100
            )
        else:
            self.server = WSGIServer(
                get_lisener(location),
                self.web_application,
                spawn=100
            )
        self.server.start()

    def run(self):
        LOGGER.info('Starting node: {}'.format(self.server_name))

        def sigterm():
            LOGGER.info('Starting SIGTERM')
            self.scheduler.shutdown(True)

        gevent_signal(signal.SIGTERM, sigterm)

        def sigusr1():
            LOGGER.info('Starting SIGUSR1')
            self.scheduler.shutdown()

        gevent_signal(signal.SIGUSR1, sigusr1)

        for auction_item in \
                iterview(self.config['main']["couch_url"],
                         self.config['main']['auctions_db'],
                         'chronograph/start_date'):
            datestamp = (
                datetime.now(self.timezone) + timedelta(minutes=1)
            ).isoformat()
            # ADD FILTER BY VALUE
            # {start: '2016-09-10T14:36:40.378777+03:00', test: false}
            if datestamp < auction_item['value']['start']:
                worker_cmd_provider = \
                    self.mapper(FeedItem(auction_item['value']))
                if not worker_cmd_provider:
                    continue
                self.scheduler.schedule_auction(
                    auction_item['id'], auction_item['value'],
                    args=worker_cmd_provider(auction_item['id'])
                )

            if self.scheduler.exit:
                break

        while not self.scheduler.execution_stopped:
            sleep(10)
            LOGGER.info('Wait until execution stopped')


def main():
    parser = argparse.ArgumentParser(
        description='---- Auctions Chronograph ----')
    parser.add_argument('config', type=str, help='Path to configuration file')
    parser.add_argument('-t', dest='check', action='store_const',
                        const=True, default=False,
                        help='Services availability checks only')
    params = parser.parse_args()
    if os.path.isfile(params.config):
        with open(params.config) as config_file_obj:
            config = load(config_file_obj.read())
        logging.config.dictConfig(config)
        chronograph = AuctionsChronograph(config)
        if params.check:
            exit()
        chronograph.run()


if __name__ == '__main__':
    main()
