# TODO: test chronograph config
import webtest
import pytest
from requests import Session
from gevent import spawn

from openprocurement.auction.chronograph import AuctionsChronograph
from openprocurement.auction.helpers.chronograph import check_auction_workers
from openprocurement.auction.tests.utils import put_test_doc, test_public_document, \
    update_start_auction_period

test_chronograph_config = {}


@pytest.fixture(scope='funtion')
def chronograph(request):
    # webapp = true
    chrono = AuctionsChronograph(test_chronograph_config)
    request.cls.chrono = chrono
    request.client = Session() # TODO: Add prefix path
    return chrono


@pytest.mark.skip
class TestChronograph(object):

    def test_view_job_add(self):
        spawn(self.chrono.run())
        with put_test_dock(some_db, update_start_auction_period(test_public_document)):
            resp = self.client.get('/jobs')
            assert resp

    def test_listing(self):
        spawn(self.chrono.run())
        with put_test_dock(some_db, update_start_auction_period(test_public_document)):
            resp = self.client.get('/active_jobs')
            assert resp

    def test_shutdown(self):
        resp = self.client.get('/active_jobs')
        assert resp.test == "Start shutdown"


def test_chronograph_init_services(chronograph_config,
                                   chronograph_logger, mocker):
    chronograph_config['main']['web_app'] = 'localhost:8080'

    mock_init_database = mocker.patch.object(AuctionsChronograph, 'init_database', autospec=True)
    mock_init_database.side_effect = Exception('No route to any couchdb server')

    mock_init_scheduler = mocker.patch.object(AuctionsChronograph, 'init_scheduler', autospec=True)
    mock_init_scheduler.side_effect = Exception('Scheduler Error')

    mock_init_web_app = mocker.patch.object(AuctionsChronograph, 'init_web_app', autospec=True)
    mock_init_web_app.side_effect = Exception('Webapp Error')

    mock_check_workers = mocker.MagicMock()
    mock_check_workers.return_value = [Exception('Worker Error')]
    mocker.patch(
        "openprocurement.auction.chronograph.check_auction_workers",
        mock_check_workers
    )

    with pytest.raises(Exception) as e:
        AuctionsChronograph(chronograph_config)
    log_strings = chronograph_logger.log_capture_string.getvalue().split('\n')
    assert e.value.message == 'No route to any couchdb server'

    assert log_strings.count('No route to any couchdb server') == 1
    assert log_strings.count('Scheduler Error') == 1
    assert log_strings.count('Webapp Error') == 1

    mock_init_database.side_effect = None

    with pytest.raises(Exception) as e:
        AuctionsChronograph(chronograph_config)
    log_strings = chronograph_logger.log_capture_string.getvalue().split('\n')
    assert e.value.message == 'Scheduler Error'

    assert log_strings.count('No route to any couchdb server') == 1
    assert log_strings.count('Scheduler Error') == 2
    assert log_strings.count('Webapp Error') == 2

    mock_init_scheduler.side_effect = None

    with pytest.raises(Exception) as e:
        AuctionsChronograph(chronograph_config)
    log_strings = chronograph_logger.log_capture_string.getvalue().split('\n')
    assert e.value.message == 'Webapp Error'

    assert log_strings.count('No route to any couchdb server') == 1
    assert log_strings.count('Scheduler Error') == 2
    assert log_strings.count('Webapp Error') == 3

    mock_init_web_app.side_effect = None

    with pytest.raises(Exception) as e:
        AuctionsChronograph(chronograph_config)
    log_strings = chronograph_logger.log_capture_string.getvalue().split('\n')
    assert e.value.message == 'Worker Error'

    assert log_strings.count('No route to any couchdb server') == 1
    assert log_strings.count('Scheduler Error') == 2
    assert log_strings.count('Webapp Error') == 3

    mock_check_workers.return_value = None

    AuctionsChronograph(chronograph_config)

    assert mock_init_database.call_count == mock_init_scheduler.call_count == \
           mock_init_web_app.call_count == mock_check_workers.call_count == 5


def test_check_auction_workers(chronograph_config,
                               chronograph_logger, mocker):

    mock_check_output = mocker.MagicMock()
    mock_check_output.side_effect = [
        Exception('Worker 1 Error'), None, Exception('Worker 3 Error')
    ]
    mocker.patch(
        "openprocurement.auction.helpers.chronograph.check_output",
        mock_check_output
    )
    worker_errors = check_auction_workers(chronograph_config['main'])

    assert mock_check_output.call_count == 3

    assert chronograph_config['main']['auction_worker'] in mock_check_output.call_args_list[0][0][0]
    assert chronograph_config['main']['auction_worker_config'] in mock_check_output.call_args_list[0][0][0]

    assert chronograph_config['main']['auction_worker'] in mock_check_output.call_args_list[1][0][0]
    assert chronograph_config['main']['auction_worker_config'] in mock_check_output.call_args_list[1][0][0]

    assert chronograph_config['main']['dgfInsider']['auction_worker'] in mock_check_output.call_args_list[2][0][0]
    assert chronograph_config['main']['dgfInsider']['auction_worker_config'] in mock_check_output.call_args_list[2][0][0]

    assert len(worker_errors) == 2

    log_strings = chronograph_logger.log_capture_string.getvalue().split('\n')
    assert log_strings.count('Worker 1 Error') == 1
    assert log_strings.count('Worker 3 Error') == 1
