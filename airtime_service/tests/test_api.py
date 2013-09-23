import json
from urllib import urlencode
from StringIO import StringIO

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
from twisted.trial.unittest import TestCase
from twisted.web.client import Agent, FileBodyProducer, readBody
from twisted.web.http_headers import Headers
from twisted.web.server import Site

from airtime_service.api import AirtimeServiceApp
from airtime_service.models import VoucherPool


class TestVoucherPool(TestCase):
    timeout = 5

    @inlineCallbacks
    def setUp(self):
        self.asapp = AirtimeServiceApp("sqlite://", reactor=reactor)
        site = Site(self.asapp.app.resource(), 'localhost')
        self.listener = reactor.listenTCP(0, site)
        self.listener_port = self.listener.getHost().port
        # Keep a connection around so the in-memory db doesn't go away.
        self.conn = yield self.asapp.engine.connect()

    def tearDown(self):
        return self.listener.loseConnection()

    def populate_pool(self, pool_name, operators, denominations, suffixes):
        pool = VoucherPool(pool_name, self.conn)
        return pool.import_vouchers([
            {
                'operator': operator,
                'denomination': denomination,
                'voucher': '%s-%s-%s' % (operator, denomination, suffix),
            }
            for operator in operators
            for denomination in denominations
            for suffix in suffixes
        ])

    def make_url(self, url_path):
        return 'http://localhost:%s/%s' % (
            self.listener_port, url_path.lstrip('/'))

    def post(self, url_path, params, expected_code=200):
        agent = Agent(reactor)
        url = self.make_url(url_path)
        headers = Headers({
            'Content-Type': ['application/x-www-form-urlencoded'],
        })
        body = FileBodyProducer(StringIO(urlencode(params)))
        d = agent.request('POST', url, headers, body)
        return d.addCallback(self._get_response_body, expected_code)

    def _get_response_body(self, response, expected_code):
        assert response.code == expected_code
        return readBody(response).addCallback(json.loads)

    def post_issue(self, operator, denomination, request_id,
                   expected_code=200):
        params = {
            'operator': operator,
            'denomination': denomination,
            'request_id': request_id,
        }
        return self.post('testpool/issue', params, expected_code)

    @inlineCallbacks
    def test_issue_missing_pool(self):
        rsp = yield self.post_issue('Tank', 'red', 'req-0', expected_code=404)
        assert rsp == {
            'request_id': 'req-0',
            'error': 'Voucher pool does not exist.',
        }

    @inlineCallbacks
    def test_issue(self):
        yield self.populate_pool('testpool', ['Tank'], ['red'], [0, 1])
        rsp0 = yield self.post_issue('Tank', 'red', 'req-0')
        assert set(rsp0.keys()) == set(['request_id', 'voucher'])
        assert rsp0['request_id'] == 'req-0'
        assert rsp0['voucher'] in ['Tank-red-0', 'Tank-red-1']

        rsp1 = yield self.post_issue('Tank', 'red', 'req-1')
        assert set(rsp1.keys()) == set(['request_id', 'voucher'])
        assert rsp1['request_id'] == 'req-1'
        assert rsp1['voucher'] in ['Tank-red-0', 'Tank-red-1']

        assert rsp0['voucher'] != rsp1['voucher']

    @inlineCallbacks
    def test_issue_no_voucher(self):
        yield self.populate_pool('testpool', ['Tank'], ['red'], [0])
        rsp = yield self.post_issue('Tank', 'blue', 'req-0')
        assert rsp == {
            'request_id': 'req-0',
            'error': 'No voucher available.',
        }
