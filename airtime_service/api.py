from StringIO import StringIO
import csv
from functools import partial, update_wrapper
from hashlib import md5
import json

from klein import Klein

from twisted.internet.defer import inlineCallbacks, returnValue, maybeDeferred
from twisted.python import log

from airtime_service.models import (
    get_engine, VoucherPool, NoVoucherPool, NoVoucherAvailable, AuditMismatch,
)


class APIError(Exception):
    code = 500

    def __init__(self, message, code=None):
        super(APIError, self).__init__(message)
        if code is not None:
            self.code = code


class BadRequestParams(APIError):
    code = 400


def service(cls):
    cls.app = Klein()
    for attr in dir(cls):
        meth = getattr(cls, attr)
        handler_args = getattr(meth, '_handler_args', None)
        if handler_args is not None:
            wrapper = partial(_handler_wrapper, meth)
            update_wrapper(wrapper, meth)
            route = cls.app.route(*handler_args[0], **handler_args[1])
            wrapper = route(wrapper)
            setattr(cls, attr, wrapper)
    return cls


def handler(*args, **kw):
    def deco(func):
        func._handler_args = (args, kw)
        return func
    return deco


def _handler_wrapper(func, self, request, *args, **kw):
    d = maybeDeferred(func, self, request, *args, **kw)
    d.addErrback(self.handle_api_error, request)
    return d


@service
class AirtimeServiceApp(object):
    def __init__(self, conn_str, reactor):
        self.engine = get_engine(conn_str, reactor)

    def handle_api_error(self, failure, request):
        # failure.printTraceback()
        error = failure.value
        if failure.check(NoVoucherPool):
            error = APIError('Voucher pool does not exist.', 404)
        elif failure.check(AuditMismatch):
            error = BadRequestParams(
                "This request has already been performed with different"
                " parameters.")
        elif not failure.check(APIError):
            log.err(failure)
            error = APIError('Internal server error.')
        return self.format_error(request, error)

    def _set_request_id(self, request, request_id):
        # We name-mangle the attr because `request` isn't our object.
        request.__request_id = request_id

    def _get_request_id(self, request):
        try:
            return request.__request_id
        except AttributeError:
            return None

    def format_response(self, request, **params):
        request.setHeader('Content-Type', 'application/json')
        params['request_id'] = self._get_request_id(request)
        return json.dumps(params)

    def format_error(self, request, error):
        request.setHeader('Content-Type', 'application/json')
        request.setResponseCode(error.code)
        return json.dumps({
            'request_id': self._get_request_id(request),
            'error': error.message,
        })

    def _get_params(self, params, mandatory, optional):
        missing = set(mandatory) - set(params.keys())
        extra = set(params.keys()) - (set(mandatory) | set(optional))
        if missing:
            raise BadRequestParams("Missing request parameters: '%s'" % (
                "', '".join(sorted(missing))))
        if extra:
            raise BadRequestParams("Unexpected request parameters: '%s'" % (
                "', '".join(sorted(extra))))
        return params

    def get_json_params(self, request, mandatory, optional=()):
        return self._get_params(
            json.loads(request.content.read()), mandatory, optional)

    def get_url_params(self, request, mandatory, optional=()):
        if 'request_id' in request.args:
            self._set_request_id(request, request.args['request_id'][0])
        params = self._get_params(request.args, mandatory, optional)
        return dict((k, v[0]) for k, v in params.iteritems())

    @handler(
        '/<string:voucher_pool>/issue/<string:operator>/<string:request_id>',
        methods=['PUT'])
    @inlineCallbacks
    def issue_voucher(self, request, voucher_pool, operator, request_id):
        self._set_request_id(request, request_id)
        params = self.get_json_params(
            request, ['transaction_id', 'user_id', 'denomination'])
        audit_params = {
            'request_id': request_id,
            'transaction_id': params.pop('transaction_id'),
            'user_id': params.pop('user_id'),
        }
        conn = yield self.engine.connect()
        pool = VoucherPool(voucher_pool, conn)
        try:
            voucher = yield pool.issue_voucher(
                operator, params['denomination'], audit_params)
        except NoVoucherAvailable:
            # This is a normal condition, so we still return a 200 OK.
            raise APIError('No voucher available.', 200)
        finally:
            yield conn.close()

        returnValue(self.format_response(request, voucher=voucher['voucher']))

    @handler('/<string:voucher_pool>/audit_query', methods=['GET'])
    @inlineCallbacks
    def audit_query(self, request, voucher_pool):
        params = self.get_url_params(
            request, ['field', 'value'], ['request_id'])
        if params['field'] not in ['request_id', 'transaction_id', 'user_id']:
            raise BadRequestParams('Invalid audit field.')

        conn = yield self.engine.connect()
        pool = VoucherPool(voucher_pool, conn)
        try:
            query = {
                'request_id': pool.query_by_request_id,
                'transaction_id': pool.query_by_transaction_id,
                'user_id': pool.query_by_user_id,
            }[params['field']]
            rows = yield query(params['value'])
        finally:
            yield conn.close()

        results = [{
            'request_id': row['request_id'],
            'transaction_id': row['transaction_id'],
            'user_id': row['user_id'],
            'request_data': row['request_data'],
            'response_data': row['response_data'],
            'error': row['error'],
            'created_at': row['created_at'].isoformat(),
        } for row in rows]
        returnValue(self.format_response(request, results=results))

    @handler(
        '/<string:voucher_pool>/import/<string:request_id>', methods=['PUT'])
    @inlineCallbacks
    def import_vouchers(self, request, voucher_pool, request_id):
        self._set_request_id(request, request_id)
        content_md5 = request.requestHeaders.getRawHeaders('Content-MD5')
        if content_md5 is None:
            raise BadRequestParams("Missing Content-MD5 header.")
        content_md5 = content_md5[0].lower()
        content = request.content.read()
        if content_md5 != md5(content).hexdigest().lower():
            raise BadRequestParams(
                "Content-MD5 header does not match content.")

        reader = csv.DictReader(StringIO(content))
        row_iter = lowercase_row_keys(reader)

        conn = yield self.engine.connect()
        pool = VoucherPool(voucher_pool, conn)
        try:
            yield pool.import_vouchers(request_id, content_md5, row_iter)
        finally:
            yield conn.close()

        request.setResponseCode(201)
        returnValue(self.format_response(request, imported=True))

    @handler('/<string:voucher_pool>/voucher_counts', methods=['GET'])
    @inlineCallbacks
    def voucher_counts(self, request, voucher_pool):
        # This sets the request_id on the request object.
        self.get_url_params(request, [], ['request_id'])

        conn = yield self.engine.connect()
        pool = VoucherPool(voucher_pool, conn)
        try:
            rows = yield pool.count_vouchers()
        finally:
            yield conn.close()

        if rows:
            print rows[0].keys()
        results = [{
            'operator': row['operator'],
            'denomination': row['denomination'],
            'used': row['used'],
            'count': row['count'],
        } for row in rows]
        returnValue(self.format_response(request, voucher_counts=results))

    @handler(
        '/<string:voucher_pool>/export/<string:request_id>', methods=['PUT'])
    @inlineCallbacks
    def export_vouchers(self, request, voucher_pool, request_id):
        self._set_request_id(request, request_id)
        params = self.get_json_params(
            request, [], ['count', 'operators', 'denominations'])
        conn = yield self.engine.connect()
        pool = VoucherPool(voucher_pool, conn)
        try:
            response = yield pool.export_vouchers(
                request_id, params.get('count'), params.get('operators'),
                params.get('denominations'))
        finally:
            yield conn.close()

        returnValue(self.format_response(
            request, vouchers=response['vouchers'],
            warnings=response['warnings']))


def lowercase_row_keys(rows):
    for row in rows:
        yield dict((k.lower(), v) for k, v in row.iteritems())
