try:
    import urllib3.contrib.pyopenssl
    urllib3.contrib.pyopenssl.inject_into_urllib3()
except ImportError:
    pass

import iso8601
import inspect
import os
import uuid
import logging
import json
import requests
import yaml

from retrying import retry
from datetime import MINYEAR, datetime
from pytz import timezone
from gevent import sleep
from hashlib import sha1
from redis import Redis
from redis.sentinel import Sentinel
from pkg_resources import parse_version
from restkit.wrappers import BodyWrapper
from barbecue import chef
from fractions import Fraction
from munch import Munch
from zope.interface import implementer
from urlparse import urljoin

from openprocurement.auction.interfaces import IFeedItem
from openprocurement.auction.constants import DEFAULT_CONFIG


logger = logging.getLogger('Auction Worker')

EXTRA_LOGGING_VALUES = {
    'X-Request-ID': 'JOURNAL_REQUEST_ID',
    'X-Clint-Request-ID': 'JOURNAL_CLIENT_REQUEST_ID'
}


logging.addLevelName(25, 'CHECK')


def check(self, msg, exc=None, *args, **kwargs):
    self.log(25, msg)
    if exc:
        self.error(exc, exc_info=True)


logging.Logger.check = check


def generate_request_id(prefix=b'auction-req-'):
    return prefix + str(uuid.uuid4()).encode('ascii')


def filter_by_bidder_id(bids, bidder_id):
    """
    >>> bids = [
    ...     {"bidder_id": "1", "amount": 100},
    ...     {"bidder_id": "1", "amount": 200},
    ...     {"bidder_id": "2", "amount": 101}
    ... ]

    >>> filter_by_bidder_id(bids, "1")
    [{'amount': 100, 'bidder_id': '1'}, {'amount': 200, 'bidder_id': '1'}]

    >>> filter_by_bidder_id(bids, "2")
    [{'amount': 101, 'bidder_id': '2'}]

    """
    return [bid for bid in bids if bid['bidder_id'] == bidder_id]


def filter_start_bids_by_bidder_id(bids, bidder):
    """
    """
    return [bid for bid in bids
            if bid['bidders'][0]['id']['name'] == bidder]


def get_time(item):
    """
    >>> date = get_time({"time": "2015-01-04T15:40:44Z"}) # doctest: +NORMALIZE_WHITESPACE
    >>> date.utctimetuple()  # doctest: +NORMALIZE_WHITESPACE
    time.struct_time(tm_year=2015, tm_mon=1, tm_mday=4, tm_hour=15, tm_min=40,
                     tm_sec=44, tm_wday=6, tm_yday=4, tm_isdst=0)

    >>> date = get_time({"date": "2015-01-04T15:40:44Z"})
    >>> date.utctimetuple()  # doctest: +NORMALIZE_WHITESPACE
    time.struct_time(tm_year=2015, tm_mon=1, tm_mday=4, tm_hour=15, tm_min=40,
                     tm_sec=44, tm_wday=6, tm_yday=4, tm_isdst=0)

    >>> date = get_time({})
    >>> date.utctimetuple()  # doctest: +NORMALIZE_WHITESPACE
    time.struct_time(tm_year=0, tm_mon=12, tm_mday=31, tm_hour=21, tm_min=58,
                     tm_sec=0, tm_wday=6, tm_yday=366, tm_isdst=0)
    """
    if item.get('time', ''):
        bid_time = iso8601.parse_date(item['time'])
    elif item.get('date', ''):
        bid_time = iso8601.parse_date(item['date'])
    else:
        bid_time = datetime(MINYEAR, 1, 1, tzinfo=timezone('Europe/Kiev'))
    return bid_time


def sorting_by_amount(bids, reverse=False):
    """
    This function sort element by amount ascending.
    If amount of bids is equal it will sort it by time in descending order
    >>> bids = [
    ...     {'amount': 3966.0, 'bidder_id': 'df2', 'time': '2015-04-24T11:07:30.723296+03:00'},
    ...     {'amount': 3955.0, 'bidder_id': 'df1', 'time': '2015-04-24T11:07:30.723296+03:00'},
    ...     {'amount': 3955.0, 'bidder_id': 'df4', 'time': '2015-04-24T15:48:41.971644+03:00'}
    ... ]
    >>> sorting_by_amount(bids)  # doctest: +NORMALIZE_WHITESPACE
    [{'amount': 3955.0, 'bidder_id': 'df4', 'time': '2015-04-24T15:48:41.971644+03:00'},
     {'amount': 3955.0, 'bidder_id': 'df1', 'time': '2015-04-24T11:07:30.723296+03:00'},
     {'amount': 3966.0, 'bidder_id': 'df2', 'time': '2015-04-24T11:07:30.723296+03:00'}]
    >>> bids = [
    ...     {'amount': 3966.0, 'bidder_id': 'df1', 'time': '2015-04-24T11:07:20+03:00'},
    ...     {'amount': 3966.0, 'bidder_id': 'df2', 'time': '2015-04-24T11:07:30+03:00'},
    ...     {'amount': 3966.0, 'bidder_id': 'df4', 'time': '2015-04-24T11:07:40+03:00'},
    ... ]
    >>> sorting_by_amount(bids)  # doctest: +NORMALIZE_WHITESPACE
    [{'amount': 3966.0, 'bidder_id': 'df4', 'time': '2015-04-24T11:07:40+03:00'},
     {'amount': 3966.0, 'bidder_id': 'df2', 'time': '2015-04-24T11:07:30+03:00'},
     {'amount': 3966.0, 'bidder_id': 'df1', 'time': '2015-04-24T11:07:20+03:00'}]
    """
    def bids_compare(bid1, bid2):
        if "amount_features" in bid1 and "amount_features" in bid2:
            full_amount_bid1 = Fraction(bid1["amount_features"])
            full_amount_bid2 = Fraction(bid2["amount_features"])
        else:
            full_amount_bid1 = bid1["amount"]
            full_amount_bid2 = bid2["amount"]
        if full_amount_bid1 == full_amount_bid2:
            time_of_bid1 = get_time(bid1)
            time_of_bid2 = get_time(bid2)
            return cmp(time_of_bid2, time_of_bid1)
        else:
            return cmp(full_amount_bid1, full_amount_bid2)

    return sorted(bids, reverse=reverse, cmp=bids_compare)


def sorting_start_bids_by_amount(bids, features=None, reverse=True):
    """
    >>> bids = [
    ...  {
    ...    "date": "2014-11-19T08:22:21.726234+00:00",
    ...    "id": "d3ba84c66c9e4f34bfb33cc3c686f137",
    ...    "value": {
    ...      "currency": None,
    ...      "amount": 475000.0,
    ...      "valueAddedTaxIncluded": True
    ...    }
    ...   },
    ...  {
    ...    "date": "2014-11-19T08:22:24.038426+00:00",
    ...    "id": "5675acc9232942e8940a034994ad883e",
    ...    "value": {
    ...      "currency": None,
    ...      "amount": 480000.0,
    ...      "valueAddedTaxIncluded": True
    ...    }
    ...  }
    ... ]
    >>> sorted_data = sorting_start_bids_by_amount(bids)
    """
    def get_amount(item):
        return item['value']['amount']

    # return sorted(bids, key=get_amount, reverse=reverse)
    return chef(bids, features=features)


def sorting_by_time(bids, reverse=True):
    return sorted(bids, key=get_time, reverse=reverse)


def get_latest_bid_for_bidder(bids, bidder_id):
    return sorted(filter_by_bidder_id(bids, bidder_id),
                  key=get_time, reverse=True)[0]


def get_latest_start_bid_for_bidder(bids, bidder):
    return sorted(filter_start_bids_by_bidder_id(bids, bidder),
                  key=get_time, reverse=True)[0]


def get_tender_data(tender_url, user="", password="", retry_count=10,
                    request_id=None, session=requests):
    if not request_id:
        request_id = generate_request_id()
    extra_headers = {'content-type': 'application/json', 'X-Client-Request-ID': request_id}

    if user or password:
        auth = (user, password)
    else:
        auth = None
    for iteration in xrange(retry_count):
        try:
            logging.info("Get data from {}".format(tender_url),
                         extra={"JOURNAL_REQUEST_ID": request_id})
            response = session.get(tender_url, auth=auth, headers=extra_headers,
                                   timeout=300)
            if response.ok:
                logging.info("Response from {}: status: {} text: {}".format(
                    tender_url, response.status_code, response.text),
                    extra={"JOURNAL_REQUEST_ID": request_id}
                )
                return response.json()
            else:
                logging.error("Response from {}: status: {} text: {}".format(
                    tender_url, response.status_code, response.text),
                    extra={"JOURNAL_REQUEST_ID": request_id}
                )
                if response.status_code == 403:
                    for error in response.json()["errors"]:
                        if error["description"].startswith('Can\'t get auction info'):
                            return None
        except requests.exceptions.RequestException, e:
            logging.error(
                "Request error {} error: {}".format(tender_url, e),
                extra={"JOURNAL_REQUEST_ID": request_id}
            )
        except Exception, e:
            logging.error(
                "Unhandled error {} error: {}".format(tender_url, e),
                extra={"JOURNAL_REQUEST_ID": request_id}
            )
        logging.info("Wait before retry...",
                     extra={"JOURNAL_REQUEST_ID": request_id})
        sleep(pow(iteration, 2))
    return None


def make_request(url, data=None, files=None, user="", password="",
                 retry_count=10, method='patch', request_id=None, session=None):
    if not session:
        session = requests.Session()
    if not request_id:
        request_id = generate_request_id()
    extra_headers = {'X-Client-Request-ID': request_id}
    if data:
        extra_headers['content-type'] = 'application/json'

    if user or password:
        auth = (user, password)
    else:
        auth = None
    for iteration in xrange(retry_count):
        try:
            if data:
                response = getattr(session, method)(
                    url,
                    auth=auth,
                    headers=extra_headers,
                    data=json.dumps(data),
                    timeout=300
                )
            else:
                response = getattr(session, method)(
                    url,
                    auth=auth,
                    headers=extra_headers,
                    files=files,
                    timeout=300
                )

            if response.ok:
                logging.info("Response from {}: status: {} text: {}".format(
                    url, response.status_code, response.text),
                    extra={"JOURNAL_REQUEST_ID": request_id}
                )
                return response.json()
            elif response.status_code == 412 and response.text:
                get_tender_data(url, user=user, password=password,
                                request_id=request_id, session=session)
            elif response.status_code == 403:
                logging.info("Response from {}: status: {} text: {}".format(
                    url, response.status_code, response.text),
                    extra={"JOURNAL_REQUEST_ID": request_id}
                )
                return None
            else:
                logging.error("Response from {}: status: {} text: {}".format(
                    url, response.status_code, response.text),
                    extra={"JOURNAL_REQUEST_ID": request_id}
                )
        except requests.exceptions.RequestException, e:
            logging.error("Request error {} error: {}".format(
                url,
                e),
                extra={"JOURNAL_REQUEST_ID": request_id}
            )
        except Exception, e:
            logging.error("Unhandled error {} error: {}".format(
                url,
                e),
                extra={"JOURNAL_REQUEST_ID": request_id}
            )
        logging.info("Wait before retry...",
                     extra={"JOURNAL_REQUEST_ID": request_id})
        sleep(pow(iteration, 2))


def do_until_success(func, args=(), kw={}, repeat=10):
    for iteration in xrange(repeat):
        try:
            return func(*args, **kw)
        except Exception, e:
            logging.error("Error {} while call {} with args: {}, kw: {}".format(
                e, func, args, kw
            ))
        repeat -= 1
        if repeat == 0:
            logging.error("Stop running {} with args: {}, kw: {}".format(
                func, args, kw
            ))
            break
        sleep(pow(iteration, 2))


def calculate_hash(bidder_id, hash_secret):
    digest = sha1(hash_secret)
    digest.update(bidder_id)
    return digest.hexdigest()


def get_database(config, master=True):
    if config.get('sentinel', False):
        sentinal = Sentinel(config['sentinel'], socket_timeout=0.1,
                            password=config['redis_password'], db=config['redis_database'])
        if master:
            return sentinal.master_for(config['sentinel_cluster_name'])
        else:
            return sentinal.slave_for(config['sentinel_cluster_name'])
    else:
        return Redis.from_url(config['redis'])


@retry(stop_max_attempt_number=3)
def create_mapping(config, auction_id, auction_url):
    return get_database(config).set(auction_id, auction_url)


@retry(stop_max_attempt_number=3)
def get_mapping(config, auction_id, master=False):
    return get_database(config).get(auction_id)


@retry(stop_max_attempt_number=3)
def delete_mapping(config, auction_id):
    return get_database(config).delete(auction_id)


def prepare_extra_journal_fields(headers):
    extra = {}
    for key in EXTRA_LOGGING_VALUES:
        if key in headers:
            extra[EXTRA_LOGGING_VALUES[key]] = headers[key]
    return extra


class StreamWrapper(BodyWrapper):
    """Stream Wrapper fot Proxy Reponse"""
    stop_stream = False

    def __init__(self, resp, connection):
        super(StreamWrapper, self).__init__(resp, connection)

    def close(self):
        """ release connection """
        if self._closed:
            return
        self.eof = True
        self.resp.should_close = True

        if not self.eof:
            self.body.read()
        self.connection.release(True)
        self._closed = True

    def next(self):
        if not self.stop_stream:
            try:
                return super(StreamWrapper, self).next()
            except Exception:
                raise StopIteration


def get_bidder_id(app, session):
    if 'remote_oauth' in session and 'client_id' in session:
        if session['remote_oauth'] in app.logins_cache:
            return app.logins_cache[session['remote_oauth']]
        else:
            resp = app.remote_oauth.get('me')
            if resp.status == 200:
                app.logins_cache[session['remote_oauth']] = resp.data
                return resp.data
            else:
                return False


def unsuported_browser(request):
    if request.user_agent.browser == 'msie':
        if parse_version(request.user_agent.version) <= parse_version('9'):
            return True
        # Add to blacklist IE11
        if parse_version(request.user_agent.version) >= parse_version('11'):
            return True
    elif request.user_agent.browser == 'opera':
        if 'Opera Mini' in request.user_agent.string:
            return True
    return False


def filter_amount(stage):
    if 'amount' in stage:
        del stage['amount']
    if 'coeficient' in stage:
        del stage['coeficient']
    return stage


def get_auction_worker_configuration_path(_for, view_value, config, key='api_version'):
    value = view_value.get(key, '')
    if value:
        path = config.get(
            'auction_worker_config_for_{}_{}'.format(key, value),
            config['auction_worker_config']
        )
        return path
    else:
        return config['auction_worker_config']


def _get_worker_config_by_pmt(_for, item):
    plugin = _for.mapper.pmt_configurator.get(item.get('procurementMethodType'), DEFAULT_CONFIG)
    config = _for.mapper.plugins.get(plugin)
    return config


def prepare_auction_worker_cmd(_for, tender_id, cmd, item,
                               lot_id='', with_api_version=''):
    config = _get_worker_config_by_pmt(_for, item)
    params = [
        config['auction_worker'],
        cmd, tender_id,
        get_auction_worker_configuration_path(_for, item, config)
    ]
    if lot_id:
        params += ['--lot', lot_id]

    if with_api_version:
        params += ['--with_api_version', with_api_version]
    return params


def is_document_locked(_for, item):
    auction_id = item.id
    config = _get_worker_config_by_pmt(_for, item)

    worker_defaults_path = get_auction_worker_configuration_path(
        _for, item, config, key='api_version'
    )
    worker_defaults = yaml.load(open(worker_defaults_path))
    auction_path = get_mapping(worker_defaults, auction_id)
    if not auction_path:
        return False

    auction_path = urljoin(auction_path, '/health')
    response = make_request(url=auction_path, method="get", retry_count=5)
    if not response:
        delete_mapping(worker_defaults, auction_id)
        return False

    return True


@implementer(IFeedItem)
class FeedItem(Munch):
    """"""


def get_logger_for_calling_module():
    frame = inspect.stack()[2]
    module = inspect.getmodule(frame[0])
    return logging.getLogger(module.__name__)


def check_workers(plugins):
    exceptions = []
    logger = get_logger_for_calling_module()
    for plugin in plugins:
        try:
            if not os.path.isfile(plugins[plugin].get('auction_worker_config')):
                raise OSError('Worker config for {} auctions does not exists'.format(plugin))
        except OSError as e:
            exceptions.append(e)
            result = ('failed', e)
        else:
            result = ('ok', None)
        logger.check('{} auctions worker config - {}'.format(plugin, result[0]), result[1])

    if exceptions:
        raise exceptions[0]
