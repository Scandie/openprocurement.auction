import logging
import iso8601
from urlparse import urljoin

from couchdb import Server, Session
from requests import request, Session as RequestsSession
from yaml import safe_dump as yaml_dump

from openprocurement.auction.utils import generate_request_id, make_request
from openprocurement.auction.worker_core.constants import TIMEZONE
from openprocurement.auction.worker_core.journal import (
    AUCTION_WORKER_API_AUDIT_LOG_APPROVED,
    AUCTION_WORKER_API_AUDIT_LOG_NOT_APPROVED,
)


LOGGER = logging.getLogger("Auction Worker")


class RequestIDServiceMixin(object):
    """ Simple mixin class """
    def generate_request_id(self):
        self.request_id = generate_request_id()


class AuditServiceMixin(object):
    """ Mixin class to create, modify and upload audit documents"""
    def prepare_audit(self):
        raise NotImplementedError

    def approve_audit_info_on_announcement(self, approved={}):
        raise NotImplementedError

    def upload_audit_file_with_document_service(self, doc_id=None):
        files = {'file': ('audit_{}.yaml'.format(self.auction_doc_id),
                          yaml_dump(self.audit, default_flow_style=False))}
        ds_response = make_request(self.worker_defaults["DOCUMENT_SERVICE"]["url"],
                                   files=files, method='post',
                                   user=self.worker_defaults["DOCUMENT_SERVICE"]["username"],
                                   password=self.worker_defaults["DOCUMENT_SERVICE"]["password"],
                                   session=self.session_ds, retry_count=3)

        if doc_id:
            method = 'put'
            path = self.tender_url + '/documents/{}'.format(doc_id)
        else:
            method = 'post'
            path = self.tender_url + '/documents'

        response = make_request(path, data=ds_response,
                                user=self.worker_defaults["resource_api_token"],
                                method=method, request_id=self.request_id, session=self.session,
                                retry_count=2
                                )
        if response:
            doc_id = response["data"]['id']
            LOGGER.info(
                "Audit log approved. Document id: {}".format(doc_id),
                extra={"JOURNAL_REQUEST_ID": self.request_id,
                       "MESSAGE_ID": AUCTION_WORKER_API_AUDIT_LOG_APPROVED}
            )
            return doc_id
        else:
            LOGGER.warning(
                "Audit log not approved.",
                extra={"JOURNAL_REQUEST_ID": self.request_id,
                       "MESSAGE_ID": AUCTION_WORKER_API_AUDIT_LOG_NOT_APPROVED})

    def upload_audit_file_without_document_service(self, doc_id=None):
        files = {'file': ('audit_{}.yaml'.format(self.auction_doc_id),
                          yaml_dump(self.audit, default_flow_style=False))}
        if doc_id:
            method = 'put'
            path = self.tender_url + '/documents/{}'.format(doc_id)
        else:
            method = 'post'
            path = self.tender_url + '/documents'

        response = make_request(path, files=files,
                                user=self.worker_defaults["resource_api_token"],
                                method=method, request_id=self.request_id, session=self.session,
                                retry_count=2
                                )
        if response:
            doc_id = response["data"]['id']
            LOGGER.info(
                "Audit log approved. Document id: {}".format(doc_id),
                extra={"JOURNAL_REQUEST_ID": self.request_id,
                       "MESSAGE_ID": AUCTION_WORKER_API_AUDIT_LOG_APPROVED}
            )
            return doc_id
        else:
            LOGGER.warning(
                "Audit log not approved.",
                extra={"JOURNAL_REQUEST_ID": self.request_id,
                       "MESSAGE_ID": AUCTION_WORKER_API_AUDIT_LOG_NOT_APPROVED})


class DateTimeServiceMixin(object):
    """ Simple time convertion mixin"""

    def convert_datetime(self, datetime_stamp):
        return iso8601.parse_date(datetime_stamp).astimezone(TIMEZONE)


class InitializeServiceMixin(object):

    def init_services(self):
        exceptions = []

        init_methods = [(self.init_api, 'API'), (self.init_database, 'CouchDB')]
        if self.worker_defaults.get("with_document_service"):
            init_methods.append((self.init_ds, 'Document Service'))

        # Checking Worker services
        for method, service in init_methods:
            result = ('ok', None)
            try:
                method()
            except Exception as e:
                exceptions.append(e)
                result = ('failed', e)
            LOGGER.check('{} - {}'.format(service, result[0]), result[1])

        if exceptions:
            raise exceptions[0]

    def init_api(self):
        """
        Check API availability and set tender_url attribute
        """
        api_url = "{resource_api_server}/api/{resource_api_version}/health"
        if self.debug:
            response = True
        else:
            response = make_request(url=api_url.format(**self.worker_defaults),
                                    method="get", retry_count=5)
        if not response:
            raise Exception("API can't be reached")
        else:
            self.tender_url = urljoin(
                self.worker_defaults["resource_api_server"],
                "/api/{0}/{1}/{2}".format(
                    self.worker_defaults["resource_api_version"],
                    self.worker_defaults["resource_name"],
                    self.tender_id
                )
            )

    def init_ds(self):
        """
        Check Document service availability and set session_ds attribute
        """
        ds_config = self.worker_defaults.get("DOCUMENT_SERVICE")
        request("GET", ds_config.get("url"), timeout=5)
        self.session_ds = RequestsSession()

    def init_database(self):
        """
        Check CouchDB availability and set db attribute
        """
        server, db = self.worker_defaults.get("COUCH_DATABASE").rsplit('/', 1)
        server = Server(server, session=Session(retry_delays=range(10)))
        database = server[db] if db in server else server.create(db)
        self.db = database
