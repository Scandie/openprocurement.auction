from gevent import monkey
monkey.patch_all()

from openprocurement_client.sync import get_resource_items

try:
    import urllib3.contrib.pyopenssl
    urllib3.contrib.pyopenssl.inject_into_urllib3()
except ImportError:
    pass

import logging
import logging.config
import os
import argparse
import iso8601

from datetime import datetime
from time import sleep, mktime, time
from urlparse import urljoin

from apscheduler.schedulers.gevent import GeventScheduler
from gevent.queue import Queue, Empty
from gevent.subprocess import call, check_call
from requests.exceptions import ConnectionError

from couchdb import Database, Session
from dateutil.tz import tzlocal
from systemd_msgs_ids import (
    DATA_BRIDGE_PLANNING_START_BRIDGE,
    DATA_BRIDGE_PLANNING_DATA_SYNC,
    DATA_BRIDGE_PLANNING_TENDER_SKIP,
    DATA_BRIDGE_PLANNING_TENDER_ALREADY_PLANNED,
    DATA_BRIDGE_PLANNING_LOT_SKIP,
    DATA_BRIDGE_PLANNING_LOT_ALREADY_PLANNED,
    DATA_BRIDGE_PLANNING_SKIPED_TEST,
    DATA_BRIDGE_PLANNING_SELECT_TENDER,
    DATA_BRIDGE_PLANNING_DATA_SYNC_RESUME,
    DATA_BRIDGE_PLANNING_PROCESS,
    DATA_BRIDGE_PLANNING_SLEEP,
    DATA_BRIDGE_PLANNING_RESUME,
    DATA_BRIDGE_PLANNING_COUCH_FEED,
    DATA_BRIDGE_PLANNING_COUCH_DATA_SYNC,
    DATA_BRIDGE_RE_PLANNING_START_BRIDGE,
    DATA_BRIDGE_RE_PLANNING_TENDER_ALREADY_PLANNED,
    DATA_BRIDGE_RE_PLANNING_LOT_ALREADY_PLANNED,
    DATA_BRIDGE_RE_PLANNING_FINISHED
)
from yaml import load
from .design import endDate_view, startDate_view, PreAnnounce_view
from .utils import do_until_success

SIMPLE_AUCTION_TYPE = 0
SINGLE_LOT_AUCTION_TYPE = 1

MULTILOT_AUCTION_ID = "{0[id]}_{1[id]}"  # {TENDER_ID}_{LOT_ID}

logger = logging.getLogger(__name__)


class AuctionsDataBridge(object):

    """Auctions Data Bridge"""

    def __init__(self, config, activate=False):
        super(AuctionsDataBridge, self).__init__()
        self.config = config
        self.resource_ids_list = []
        self.activate = activate
        self.tz = tzlocal()

        self.couch_url = urljoin(
            self.config_get('couch_url'),
            self.config_get('auctions_db')
        )
        self.db = Database(self.couch_url,
                           session=Session(retry_delays=range(10)))

    def config_get(self, name):
        return self.config.get('main').get(name)

    def get_resource_items_list(self, re_planning=False):

        for item in get_resource_items(
                host=self.config_get('resource_api_server'),
                version=self.config_get('resource_api_version'),
                resource=self.config_get('resource_name'),
                key='', extra_params={'opt_fields': 'status,auctionPeriod', 'mode': '_all_'}):
            if item['status'] == "active.auction":
                if 'lots' not in item and 'auctionPeriod' in item and 'startDate' in item['auctionPeriod'] \
                        and 'endDate' not in item['auctionPeriod']:

                    start_date = iso8601.parse_date(item['auctionPeriod']['startDate'])
                    start_date = start_date.astimezone(self.tz)
                    auctions_start_in_date = startDate_view(
                        self.db,
                        key=(mktime(start_date.timetuple()) + start_date.microsecond / 1E6) * 1000
                    )
                    if datetime.now(self.tz) > start_date:
                        logger.info("Tender {} start date in past. Skip it for planning".format(item['id']),
                                    extra={'MESSAGE_ID': DATA_BRIDGE_PLANNING_TENDER_SKIP})
                        continue
                    if re_planning and item['id'] in self.resource_ids_list:
                        logger.info("Tender {} already planned while replanning".format(item['id']),
                                    extra={'MESSAGE_ID': DATA_BRIDGE_RE_PLANNING_TENDER_ALREADY_PLANNED})
                        continue
                    elif not re_planning and [row.id for row in auctions_start_in_date.rows if row.id == item['id']]:
                        logger.info("Tender {} already planned on same date".format(item['id']),
                                    extra={'MESSAGE_ID': DATA_BRIDGE_PLANNING_TENDER_ALREADY_PLANNED})
                        continue
                    yield (str(item['id']), )
                elif 'lots' in item:
                    for lot in item['lots']:
                        if lot["status"] == "active" and 'auctionPeriod' in lot \
                                and 'startDate' in lot['auctionPeriod'] and 'endDate' not in lot['auctionPeriod']:
                            start_date = iso8601.parse_date(lot['auctionPeriod']['startDate'])
                            start_date = start_date.astimezone(self.tz)
                            auctions_start_in_date = startDate_view(
                                self.db,
                                key=(mktime(start_date.timetuple()) + start_date.microsecond / 1E6) * 1000
                            )
                            if datetime.now(self.tz) > start_date:
                                logger.info(
                                    "Start date for lot {} in tender {} is in past. Skip it for planning".format(
                                        lot['id'], item['id']),
                                    extra={'MESSAGE_ID': DATA_BRIDGE_PLANNING_LOT_SKIP}
                                )
                                continue
                            auction_id = MULTILOT_AUCTION_ID.format(item, lot)
                            if re_planning and auction_id in self.resource_ids_list:
                                logger.info("Tender {} already planned while replanning".format(auction_id),
                                            extra={'MESSAGE_ID': DATA_BRIDGE_RE_PLANNING_LOT_ALREADY_PLANNED})
                                continue
                            elif not re_planning and [row.id for row in auctions_start_in_date.rows if row.id == auction_id]:
                                logger.info("Tender {} already planned on same date".format(auction_id),
                                            extra={'MESSAGE_ID': DATA_BRIDGE_PLANNING_LOT_ALREADY_PLANNED})
                                continue
                            yield (str(item["id"]), str(lot["id"]), )
            if item['status'] == "active.qualification" and 'lots' in item:
                for lot in item['lots']:
                    if lot["status"] == "active":
                        is_pre_announce = PreAnnounce_view(self.db)
                        auction_id = MULTILOT_AUCTION_ID.format(item, lot)
                        if [row.id for row in is_pre_announce.rows if row.id == auction_id]:
                            self.start_auction_worker_cmd('announce', item['id'], lot_id=lot['id'],)
            if item['status'] == "cancelled":
                future_auctions = endDate_view(
                    self.db, startkey=time() * 1000
                )
                if 'lots' in item:
                    for lot in item['lots']:
                        auction_id = MULTILOT_AUCTION_ID.format(item, lot)
                        if auction_id in [i.id for i in future_auctions]:
                            logger.info('Tender {0} selected for cancellation'.format(item['id']))
                            self.start_auction_worker_cmd('cancel', item['id'], lot_id=lot['id'])
                else:
                    if item["id"] in [i.id for i in future_auctions]:
                        logger.info('Tender {0} selected for cancellation'.format(item['id']))
                        self.start_auction_worker_cmd('cancel', item["id"])


    def start_auction_worker_cmd(self, cmd, tender_id, with_api_version=None, lot_id=None):
        params = [self.config_get('auction_worker'),
                  cmd, tender_id,
                  self.config_get('auction_worker_config')]
        if lot_id:
            params += ['--lot', lot_id]

        if with_api_version:
            params += ['--with_api_version', with_api_version]
        result = do_until_success(
            check_call,
            args=(params,),
        )

        logger.info("Auction command {} result: {}".format(params[1], result))

    def run(self):
        logger.info('Start Auctions Bridge',
                    extra={'MESSAGE_ID': DATA_BRIDGE_PLANNING_START_BRIDGE})
        logger.info('Start data sync...',
                    extra={'MESSAGE_ID': DATA_BRIDGE_PLANNING_DATA_SYNC})
        try:
            for planning_data in self.get_resource_items_list():
                if len(planning_data) == 1:
                    logger.info('Tender {0} selected for planning'.format(*planning_data))
                    self.start_auction_worker_cmd('planning', planning_data[0])
                elif len(planning_data) == 2:
                    logger.info('Lot {1} of tender {0} selected for planning'.format(*planning_data))
                    self.start_auction_worker_cmd('planning', planning_data[0], lot_id=planning_data[1])
        except ConnectionError as e:
            logger.error("Failed connect to api resource: {}".format(self.config_get('resource_api_server')))
            sleep(10)

    def run_re_planning(self):
        pass
        # self.re_planning = True
        # self.offset = ''
        # logger.info('Start Auctions Bridge for re-planning...',
        #             extra={'MESSAGE_ID': DATA_BRIDGE_RE_PLANNING_START_BRIDGE})
        # for tender_item in self.get_teders_list(re_planning=True):
        #     logger.debug('Tender {} selected for re-planning'.format(tender_item))
        #     for planning_data in self.get_teders_list():
        #         if len(planning_data) == 1:
        #             logger.info('Tender {0} selected for planning'.format(*planning_data))
        #             self.start_auction_worker_cmd('planning', planning_data[0])
        #         elif len(planning_data) == 2:
        #             logger.info('Lot {1} of tender {0} selected for planning'.format(*planning_data))
        #             self.start_auction_worker_cmd('planning', planning_data[0], lot_id=planning_data[1])
        #         self.tenders_ids_list.append(tender_item['id'])
        #     sleep(1)
        # logger.info("Re-planning auctions finished",
        #             extra={'MESSAGE_ID': DATA_BRIDGE_RE_PLANNING_FINISHED})


def main():
    parser = argparse.ArgumentParser(description='---- Auctions Bridge ----')
    parser.add_argument('config', type=str, help='Path to configuration file')
    parser.add_argument(
        '--re-planning', action='store_true', default=False,
        help='Not ignore auctions which already scheduled')
    params = parser.parse_args()
    if os.path.isfile(params.config):
        with open(params.config) as config_file_obj:
            config = load(config_file_obj.read())
        logging.config.dictConfig(config)
        if params.re_planning:
            AuctionsDataBridge(config).run_re_planning()
        else:
            AuctionsDataBridge(config).run()


##############################################################

if __name__ == "__main__":
    main()
