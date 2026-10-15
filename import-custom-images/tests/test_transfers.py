import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'import-qcow2-disk-to-oci.py'
spec = importlib.util.spec_from_file_location('transfer_module', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Transfers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / 'disk.qcow2'

    def response(self, data, headers, status=200):
        response = io.BytesIO(data)
        response.headers = headers
        response.status = status
        return response

    def partial_download(self, validator=True):
        headers = {'Content-Length': '10'}
        if validator:
            headers['ETag'] = '"version-one"'
        with patch.object(module, 'urlopen', return_value=self.response(b'0123', headers)):
            with self.assertRaisesRegex(RuntimeError, 'Download incompleto'):
                module.download_file('https://example.com/disk', self.file, lambda value: None, threading.Event())

    def test_partial_download_resumes_from_saved_offset(self):
        self.partial_download()
        headers = {'Content-Range': 'bytes 4-9/10', 'Content-Length': '6', 'ETag': '"version-one"'}
        values = []
        with patch.object(module, 'urlopen', return_value=self.response(b'456789', headers, 206)) as opened:
            module.download_file('https://example.com/disk', self.file, values.append, threading.Event())
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header('Range'), 'bytes=4-')
        self.assertEqual(request.get_header('If-range'), '"version-one"')
        self.assertEqual(self.file.read_bytes(), b'0123456789')
        self.assertEqual(values[0], 40)
        self.assertEqual(values[-1], 100)
        metadata = json.loads(module.download_metadata_path(self.file).read_text())
        self.assertTrue(metadata['completed'])
        self.assertEqual(metadata['resumed_from_bytes'], 4)

    def test_full_response_replaces_partial_when_range_ignored_or_source_changed(self):
        self.partial_download()
        with patch.object(module, 'urlopen', return_value=self.response(b'new-disk', {'Content-Length': '8', 'ETag': '"two"'})):
            module.download_file('https://example.com/disk', self.file, lambda value: None, threading.Event())
        self.assertEqual(self.file.read_bytes(), b'new-disk')
        metadata = json.loads(module.download_metadata_path(self.file).read_text())
        self.assertEqual(metadata['resumed_from_bytes'], 0)
        self.assertTrue(metadata['restart_reason'])

    def test_unsafe_partial_response_does_not_modify_existing_bytes(self):
        self.partial_download()
        for headers in (
            {'Content-Range': 'bytes 0-5/10'}, {},
            {'Content-Range': 'bytes 4-9/10', 'Content-Length': '10'},
            {'Content-Range': 'bytes 4-9/10', 'ETag': '"changed"'},
            {'Content-Range': 'bytes 4-10/11'},
        ):
            with self.subTest(headers=headers), patch.object(module, 'urlopen', return_value=self.response(b'456789', headers, 206)):
                with self.assertRaises(RuntimeError):
                    module.download_file('https://example.com/disk', self.file, lambda value: None, threading.Event())
            self.assertEqual(self.file.read_bytes(), b'0123')

    def test_partial_without_validator_is_restarted(self):
        self.partial_download(validator=False)
        with patch.object(module, 'urlopen', return_value=self.response(b'new', {'Content-Length': '3'})) as opened:
            module.download_file('https://example.com/disk', self.file, lambda value: None, threading.Event())
        self.assertIsNone(opened.call_args.args[0].get_header('Range'))
        self.assertEqual(self.file.read_bytes(), b'new')

    def test_legacy_partial_without_metadata_is_restarted(self):
        self.file.write_bytes(b'old-partial')
        with patch.object(module, 'urlopen', return_value=self.response(b'new', {})) as opened:
            module.download_file('https://example.com/disk', self.file, lambda value: None, threading.Event())
        self.assertIsNone(opened.call_args.args[0].get_header('Range'))
        self.assertEqual(self.file.read_bytes(), b'new')

    def test_416_recognizes_complete_file_only_with_matching_validator_and_size(self):
        self.partial_download()
        self.file.write_bytes(b'0123456789')
        error = module.HTTPError('https://example.com/disk', 416, 'Range',
                                 {'Content-Range': 'bytes */10', 'ETag': '"version-one"'}, io.BytesIO())
        values = []
        with patch.object(module, 'urlopen', side_effect=error) as opened:
            module.download_file('https://example.com/disk', self.file, values.append, threading.Event())
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.file.read_bytes(), b'0123456789')
        self.assertEqual(values, [100])

    def test_416_without_proof_restarts_with_full_request(self):
        self.partial_download()
        error = module.HTTPError('https://example.com/disk', 416, 'Range',
                                 {'Content-Range': 'bytes */3'}, io.BytesIO())
        with patch.object(module, 'urlopen', side_effect=[error, self.response(b'new', {})]) as opened:
            module.download_file('https://example.com/disk', self.file, lambda value: None, threading.Event())
        self.assertEqual(opened.call_count, 2)
        self.assertIsNone(opened.call_args.args[0].get_header('Range'))
        self.assertEqual(self.file.read_bytes(), b'new')

    def test_resume_uses_last_modified_when_no_strong_etag(self):
        self.partial_download()
        metadata_path = module.download_metadata_path(self.file)
        metadata = json.loads(metadata_path.read_text())
        metadata.update(etag='W/"weak"', last_modified='Wed, 14 Oct 2026 12:00:00 GMT')
        module.atomic_json(metadata_path, metadata)
        headers = {'Content-Range': 'bytes 4-9/10', 'Last-Modified': metadata['last_modified']}
        with patch.object(module, 'urlopen', return_value=self.response(b'456789', headers, 206)) as opened:
            module.download_file('https://example.com/disk', self.file, lambda value: None, threading.Event())
        self.assertEqual(opened.call_args.args[0].get_header('If-range'), metadata['last_modified'])
        self.assertEqual(self.file.read_bytes(), b'0123456789')

    def test_download_percentage_uses_bytes_and_writes_whole_disk(self):
        data = b'x' * (3 * 1024 * 1024)
        values = []
        with patch.object(module, 'urlopen', return_value=self.response(data, {'Content-Length': str(len(data))})):
            module.download_file('https://example.com/disk.qcow2', self.file, values.append, threading.Event())
        self.assertEqual(self.file.read_bytes(), data)
        self.assertTrue(any(0 < number < 100 for number in values))
        self.assertEqual(values[-1], 100)

    def test_unknown_content_length_reports_activity_without_fake_percentage(self):
        values = []
        with patch.object(module, 'urlopen', return_value=self.response(b'disk', {})):
            module.download_file('https://example.com/disk.qcow2', self.file, values.append, threading.Event())
        self.assertEqual(values, ['RUN', 'RUN', 100])

    def test_incomplete_download_is_not_marked_complete(self):
        values = []
        with patch.object(module, 'urlopen', return_value=self.response(b'partial', {'Content-Length': '100'})):
            with self.assertRaisesRegex(RuntimeError, 'Download incompleto'):
                module.download_file('https://example.com/disk.qcow2', self.file, values.append, threading.Event())
        self.assertNotIn(100, values)

    def test_upload_reads_real_child_progress_and_multipart_id(self):
        values, identifiers = [], []
        code = ('import sys,time; '
                'sys.stderr.write("Upload ID: example-upload-id\\n"); sys.stderr.flush(); '
                'sys.stderr.write("Uploading object [###] 53%\\r"); sys.stderr.flush(); time.sleep(0.1); '
                'sys.stderr.write("Uploading object [###] 100%\\n"); sys.stderr.flush()')
        module.upload_file([sys.executable, '-c', code], values.append, identifiers.append, threading.Event())
        self.assertIn(53, values)
        self.assertEqual(values[-1], 100)
        self.assertEqual(identifiers, ['example-upload-id'])

    def test_upload_failure_does_not_report_success(self):
        values = []
        code = 'import sys; sys.stderr.write("upload failed\\n"); sys.exit(1)'
        with self.assertRaisesRegex(module.CommandError, 'upload failed'):
            module.upload_file([sys.executable, '-c', code], values.append, lambda value: None, threading.Event())
        self.assertNotIn(100, values)

    def test_interrupt_terminates_upload_child(self):
        stop = threading.Event()
        timer = threading.Timer(0.1, stop.set)
        timer.start()
        try:
            started = time.monotonic()
            with self.assertRaises(module.Stopped):
                module.upload_file([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   lambda value: None, lambda value: None, stop)
            self.assertLess(time.monotonic() - started, 3)
        finally:
            timer.cancel()


if __name__ == '__main__':
    unittest.main()
