import tempfile
import uuid
import base64
import hashlib
import datetime
import zlib
import sys
import os
import six

import traceback

from collections import OrderedDict

from socket import gethostname

import fcntl

from pywb.utils.loaders import LimitReader, to_native_str
from pywb.utils.bufferedreaders import BufferedReader
from pywb.utils.timeutils import timestamp20_now

from webagg.utils import ParamFormatter

from recorder.filters import ExcludeNone


# ============================================================================
class BaseWARCWriter(object):
    WARC_RECORDS = {'warcinfo': 'application/warc-fields',
         'response': 'application/http; msgtype=response',
         'revisit': 'application/http; msgtype=response',
         'request': 'application/http; msgtype=request',
         'metadata': 'application/warc-fields',
        }

    REVISIT_PROFILE = 'http://netpreserve.org/warc/1.0/revisit/uri-agnostic-identical-payload-digest'

    BUFF_SIZE = 8192

    FILE_TEMPLATE = 'rec-{timestamp}-{hostname}.warc.gz'

    def __init__(self, gzip=True, dedup_index=None, name='recorder',
                 header_filter=ExcludeNone()):
        self.gzip = gzip
        self.dedup_index = dedup_index
        self.rec_source_name = name
        self.header_filter = header_filter
        self.hostname = gethostname()

    def ensure_digest(self, record):
        block_digest = record.rec_headers.get('WARC-Block-Digest')
        payload_digest = record.rec_headers.get('WARC-Payload-Digest')
        if block_digest and payload_digest:
            return

        block_digester = self._create_digester()
        payload_digester = self._create_digester()

        pos = record.stream.tell()

        block_digester.update(record.status_headers.headers_buff)

        while True:
            buf = record.stream.read(self.BUFF_SIZE)
            if not buf:
                break

            block_digester.update(buf)
            payload_digester.update(buf)

        record.stream.seek(pos)
        record.rec_headers['WARC-Block-Digest'] = str(block_digester)
        record.rec_headers['WARC-Payload-Digest'] = str(payload_digester)

    def _create_digester(self):
        return Digester('sha1')

    def _set_header_buff(self, record):
        exclude_list = self.header_filter(record)
        buff = record.status_headers.to_bytes(exclude_list)
        record.status_headers.headers_buff = buff

    def write_req_resp(self, req, resp, params):
        url = resp.rec_headers.get('WARC-Target-Uri')
        dt = resp.rec_headers.get('WARC-Date')

        if not req.rec_headers.get('WARC-Record-ID'):
            req.rec_headers['WARC-Record-ID'] = self._make_warc_id()

        req.rec_headers['WARC-Target-Uri'] = url
        req.rec_headers['WARC-Date'] = dt
        req.rec_headers['WARC-Type'] = 'request'
        req.rec_headers['Content-Type'] = req.content_type

        resp_id = resp.rec_headers.get('WARC-Record-ID')
        if resp_id:
            req.rec_headers['WARC-Concurrent-To'] = resp_id

        self._set_header_buff(req)
        self._set_header_buff(resp)

        self.ensure_digest(resp)

        resp = self._check_revisit(resp, params)
        if not resp:
            print('Skipping due to dedup')
            return

        formatter = ParamFormatter(params, name=self.rec_source_name)
        self._do_write_req_resp(req, resp, params, formatter)

    def _check_revisit(self, record, params):
        if not self.dedup_index:
            return record

        try:
            url = record.rec_headers.get('WARC-Target-Uri')
            digest = record.rec_headers.get('WARC-Payload-Digest')
            iso_dt = record.rec_headers.get('WARC-Date')
            result = self.dedup_index.lookup_revisit(params, digest, url, iso_dt)
        except Exception as e:
            traceback.print_exc()
            result = None

        if result == 'skip':
            return None

        if isinstance(result, tuple) and result[0] == 'revisit':
            record.rec_headers['WARC-Type'] = 'revisit'
            record.rec_headers['WARC-Profile'] = self.REVISIT_PROFILE

            record.rec_headers['WARC-Refers-To-Target-URI'] = result[1]
            record.rec_headers['WARC-Refers-To-Date'] = result[2]

        return record

    def _write_warc_record(self, out, record):
        if self.gzip:
            out = GzippingWrapper(out)

        self._line(out, b'WARC/1.0')

        for n, v in six.iteritems(record.rec_headers):
            self._header(out, n, v)

        content_type = record.content_type
        if not content_type:
            content_type = self.WARC_RECORDS[record.rec_headers['WARC-Type']]

        self._header(out, 'Content-Type', record.content_type)

        if record.rec_headers['WARC-Type'] == 'revisit':
            http_headers_only = True
        else:
            http_headers_only = False

        if record.length:
            actual_len = len(record.status_headers.headers_buff)

            if not http_headers_only:
                diff = record.stream.tell() - actual_len
                actual_len = record.length - diff

            self._header(out, 'Content-Length', str(actual_len))

            # add empty line
            self._line(out, b'')

            # write headers and buffer
            out.write(record.status_headers.headers_buff)

            if not http_headers_only:
                out.write(record.stream.read())

            # add two lines
            self._line(out, b'\r\n')
        else:
            # add three lines (1 for end of header, 2 for end of record)
            self._line(out, b'Content-Length: 0\r\n\r\n')

        out.flush()

    def _header(self, out, name, value):
        if not value:
            return

        self._line(out, (name + ': ' + str(value)).encode('latin-1'))

    def _line(self, out, line):
        out.write(line + b'\r\n')

    @staticmethod
    def _make_warc_id(id_=None):
        if not id_:
            id_ = uuid.uuid1()
        return '<urn:uuid:{0}>'.format(id_)


# ============================================================================
class GzippingWrapper(object):
    def __init__(self, out):
        self.compressor = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS + 16)
        self.out = out

    def write(self, buff):
        #if isinstance(buff, str):
        #    buff = buff.encode('utf-8')
        buff = self.compressor.compress(buff)
        self.out.write(buff)

    def flush(self):
        buff = self.compressor.flush()
        self.out.write(buff)
        self.out.flush()


# ============================================================================
class Digester(object):
    def __init__(self, type_='sha1'):
        self.type_ = type_
        self.digester = hashlib.new(type_)

    def update(self, buff):
        self.digester.update(buff)

    def __str__(self):
        return self.type_ + ':' + to_native_str(base64.b32encode(self.digester.digest()))


# ============================================================================
class MultiFileWARCWriter(BaseWARCWriter):

    def __init__(self, dir_template, filename_template=None, max_size=0,
                 *args, **kwargs):
        super(MultiFileWARCWriter, self).__init__(*args, **kwargs)

        if not filename_template:
            dir_template, filename_template = os.path.split(dir_template)
            dir_template += os.path.sep

        if not filename_template:
            filename_template = self.FILE_TEMPLATE

        self.dir_template = dir_template
        self.filename_template = filename_template
        self.max_size = max_size

        self.fh_cache = {}

    def _open_file(self, dir_):
        timestamp = timestamp20_now()

        filename = dir_ + self.filename_template.format(hostname=self.hostname,
                                                        timestamp=timestamp)

        try:
            os.makedirs(os.path.dirname(filename))
        except:
            pass

        fh = open(filename, 'a+b')
        return fh, filename

    def _close_file(self, fh):
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()

    def remove_file(self, full_dir):
        result = self.fh_cache.pop(full_dir, None)
        if result:
            out, filename = result
            self._close_file(out)

    def _do_write_req_resp(self, req, resp, params, formatter):
        full_dir = formatter.format(self.dir_template)

        result = self.fh_cache.get(full_dir)

        close_file = False

        if result:
            out, filename = result
            is_new = False
        else:
            out, filename = self._open_file(full_dir)
            is_new = True

        try:
            url = resp.rec_headers.get('WARC-Target-Uri')
            print('Writing req/resp {0} to {1} '.format(url, filename))

            start = out.tell()

            self._write_warc_record(out, resp)
            self._write_warc_record(out, req)

            out.flush()

            new_size = out.tell()

            out.seek(start)

            if self.dedup_index:
                self.dedup_index.index_records(out, params, filename=filename)

        except Exception as e:
            traceback.print_exc()
            close_file = True

        finally:
            # check for rollover
            if self.max_size and new_size > self.max_size:
                close_file = True

            if close_file:
                if is_new:
                    self._close_file(out)
                else:
                    self.remove_file(full_dir)

            elif is_new:
                fcntl.flock(out, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fh_cache[full_dir] = (out, filename)

    def close(self):
        for n, v in self.fh_cache.items():
            out, filename = v
            self._close_file(out)

        self.fh_cache = {}


# ============================================================================
class PerRecordWARCWriter(MultiFileWARCWriter):
    def __init__(self, *args, **kwargs):
        kwargs['max_size'] = 1
        super(PerRecordWARCWriter, self).__init__(*args, **kwargs)
