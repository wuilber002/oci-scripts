import argparse
from contextlib import redirect_stdout, redirect_stderr
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'import-qcow2-disk-to-oci.py'
spec = importlib.util.spec_from_file_location('import_images', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
REAL_OCI = module.oci
REAL_DOWNLOAD = module.download_file


class Cloud:
    """Backend com objetos, work requests e verificação de concorrência reais entre threads."""
    def __init__(self, exists=False):
        self.lock = threading.RLock()
        self.exists = exists
        self.objects = {}
        self.calls = []
        self.requests = {}
        self.uploads = []
        self.downloads = []
        self.download_paths = set()
        self.max_active = 0
        self.limit = 1
        self.fail_conversion = False
        self.hold_until_uploaded = 0
        self.reject_once = False
        self.reject_seen = False
        self.unknown_once = False
        self.deleted_bucket = False
        self.state_path = None
        self.active_during_upload = False

    def error(self, status, message='Not found', code='NotFound'):
        return module.CommandError(['oci'], json.dumps(dict(status=status, message=message, code=code)))

    def api(self, *command):
        with self.lock:
            self.calls.append(command)
            def value(name):
                return command[command.index(name) + 1]
            action = command[:3]
            if action == ('os', 'ns', 'get'):
                return {'data': 'namespace'}
            if action == ('os', 'bucket', 'get'):
                if not self.exists:
                    raise self.error(404)
                return {'data': {'id': 'bucket-1'}}
            if action == ('os', 'bucket', 'create'):
                self.exists = True
                return {'data': {'id': 'bucket-1'}}
            if action == ('os', 'bucket', 'delete'):
                if self.objects:
                    raise AssertionError('Bucket cannot be deleted while objects remain')
                self.exists = False
                self.deleted_bucket = True
                return {}
            if action == ('os', 'object', 'get'):
                name = value('--name')
                if name not in self.objects:
                    raise self.error(404)
                Path(value('--file')).write_bytes(self.objects[name])
                return {}
            if action == ('os', 'object', 'put'):
                self.objects[value('--name')] = Path(value('--file')).read_bytes()
                return {}
            if action == ('os', 'object', 'list'):
                prefix = value('--prefix') if '--prefix' in command else ''
                return {'data': {'objects': [{'name': name} for name in self.objects if name.startswith(prefix)]}}
            if action == ('os', 'object', 'delete'):
                self.objects.pop(value('--object-name'), None)
                return {}
            if action == ('os', 'multipart', 'list'):
                return {'data': []}
            if action == ('os', 'multipart', 'abort'):
                return {}
            if command[:4] == ('compute', 'image', 'import', 'from-object'):
                if self.reject_once and not self.reject_seen:
                    self.reject_seen = True
                    raise self.error(429, 'Too many requests', 'TooManyRequests')
                name = value('--name')
                if name not in self.objects:
                    raise AssertionError('Object must exist before import')
                # Uploads devem ter removido os arquivos locais antes de submeter.
                for filename, object_name in self.uploads:
                    if object_name == name and filename in self.download_paths:
                        if Path(filename).exists():
                            raise AssertionError('Local QCOW2 still exists after upload')
                if self.state_path:
                    local_state = json.loads(self.state_path.read_text())
                    if not any(row['status'] == 'SUBMITTING' for row in local_state['images']):
                        raise AssertionError('Persist submission intent first')
                active = sum(work['status'] == 'IN_PROGRESS' for work in self.requests.values())
                if active >= self.limit:
                    raise AssertionError('Conversion limit exceeded')
                number = len(self.requests) + 1
                identifier = f'wr-{number}'
                self.requests[identifier] = dict(status='IN_PROGRESS', polls=0,
                                                  image_id=f'image-{number}', object_name=name,
                                                  image_name=value('--display-name'),
                                                  compartment_id=value('--compartment-id'),
                                                  tags=json.loads(value('--freeform-tags')))
                self.max_active = max(self.max_active, active + 1)
                if self.unknown_once:
                    self.unknown_once = False
                    raise module.CommandError(['oci'], 'Connection timed out')
                return {'data': {'id': f'image-{number}'}, 'opc-work-request-id': identifier}
            if command[:3] == ('work-requests', 'work-request', 'get'):
                work = self.requests[value('--work-request-id')]
                work['polls'] += 1
                if len(self.uploads) >= self.hold_until_uploaded and work['polls'] >= 2:
                    work['status'] = 'FAILED' if self.fail_conversion else 'SUCCEEDED'
                return {'data': {'status': work['status'], 'percent-complete': 33 if work['status'] == 'IN_PROGRESS' else 100}}
            if command[:3] == ('compute', 'image', 'get'):
                work = next(work for work in self.requests.values() if work['image_id'] == value('--image-id'))
                return {'data': {'lifecycle-state': 'IMPORTING' if work['status'] == 'IN_PROGRESS' else 'AVAILABLE'}}
            if command[:3] == ('compute', 'image', 'list'):
                return {'data': [dict(id=work['image_id'], **{'display-name': work['image_name'],
                                 'lifecycle-state': 'IMPORTING' if work['status'] == 'IN_PROGRESS' else
                                 'AVAILABLE' if work['status'] == 'SUCCEEDED' else 'DELETED',
                                 'freeform-tags': work['tags']}) for work in self.requests.values()
                                 if work['image_name'] == value('--display-name') and
                                 work['compartment_id'] == value('--compartment-id')]}
            if command[:3] == ('work-requests', 'work-request', 'list'):
                return {'data': [dict(id=identifier, status=work['status'])
                                 for identifier, work in self.requests.items()
                                 if work['image_id'] == value('--resource-id')]}
            raise AssertionError(command)

    def download(self, url, filename, progress, stop):
        with self.lock:
            self.downloads.append(url)
            self.download_paths.add(str(filename))
        progress(53)
        Path(filename).write_bytes(b'KDMV\x03\x00\x00\x00fixture' if url.endswith('.vmdk') else b'QFI\xfb\x00\x00\x00\x03fixture')
        progress(100)

    def upload(self, command, progress, multipart, stop):
        filename = command[command.index('--file') + 1]
        name = command[command.index('--name') + 1]
        progress(40)
        with self.lock:
            self.active_during_upload |= any(work['status'] == 'IN_PROGRESS' for work in self.requests.values())
            self.objects[name] = Path(filename).read_bytes()
            self.uploads.append((filename, name))
        progress(100)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        working_directory = patch.object(module.Path, 'cwd', return_value=self.root)
        working_directory.start()
        self.addCleanup(working_directory.stop)
        self.cloud = Cloud()
        self.out = io.StringIO()
        self.output = redirect_stdout(self.out)
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.error_output = redirect_stderr(io.StringIO())
        self.error_output.__enter__()
        self.addCleanup(self.error_output.__exit__, None, None, None)
        for name, function in [('oci', self.cloud.api), ('download_file', self.cloud.download), ('upload_file', self.cloud.upload)]:
            mocker = patch.object(module, name, side_effect=function)
            mocker.start()
            self.addCleanup(mocker.stop)
        self.args = argparse.Namespace(mode='batch', results_file=str(self.root / 'state.json'),
                                       transfer_workers=2, upload_workers=2, max_in_progress=1,
                                       poll_interval=0.01, capacity_timeout=1)
        self.cloud.state_path = Path(self.args.results_file)
        self.images = [dict(source=f'https://example.com/image-{number}.qcow2',
                            compartment_id='ocid1.compartment.oc1..test', image_name=name)
                       for number, name in enumerate(('custom-image-debian-13', 'custom-image-debian-12', 'custom-image-debian-11'))]

    def engine(self, count=3):
        return module.Importer(copy.deepcopy(self.images[:count]), self.args)

    def test_runs_until_every_conversion_and_cleanup_finish(self):
        engine = self.engine()
        self.assertEqual(engine.run(), 0)
        self.assertEqual(self.cloud.max_active, 1)
        self.assertEqual(len(self.cloud.requests), 3)
        self.assertTrue(all(row['status'] == 'DONE' and row['cleanup'] == 100 for row in engine.state['images']))
        self.assertTrue(all(not Path(filename).exists() for filename, _ in self.cloud.uploads))
        self.assertTrue(self.cloud.deleted_bucket)
        self.assertFalse(self.cloud.objects)
        state = engine.state
        self.assertTrue(all(row['work_request_id'] and row['image_id'] for row in state['images']))
        summary = self.out.getvalue().split('Imagens criadas:\n')[1].split('Log:')[0]
        self.assertIn('| Image Name', summary)
        self.assertIn('Image OCID', summary)
        for row in state['images']:
            self.assertIn(row['image_name'], summary)
            self.assertIn(row['image_id'], summary)

    def test_created_images_table_only_includes_successful_conversions(self):
        records = [dict(image_name='converted', image_id='ocid1.image.converted', status='CONVERTED'),
                   dict(image_name='done', image_id='ocid1.image.done', status='DONE'),
                   dict(image_name='failed', image_id='ocid1.image.failed', status='FAILED'),
                   dict(image_name='pending', image_id='ocid1.image.pending', status='CONVERTING')]
        table = module.render_created_images(records)
        self.assertIn('ocid1.image.converted', table)
        self.assertIn('ocid1.image.done', table)
        self.assertNotIn('ocid1.image.failed', table)
        self.assertNotIn('ocid1.image.pending', table)
        self.assertEqual(len({module.display_width(line) for line in table.splitlines()}), 1)
        self.assertEqual(module.render_created_images(records[2:]), '')

    def test_transfers_continue_while_conversion_slots_are_full(self):
        # A primeira conversão só termina quando TODOS os uploads estiverem prontos.
        # Se transferências aguardassem vaga, este teste não conseguiria terminar.
        self.cloud.hold_until_uploaded = 3
        self.args.transfer_workers = 1
        original = self.cloud.upload
        first_submitted = threading.Event()
        original_api = self.cloud.api

        def api(*command):
            result = original_api(*command)
            if command[:4] == ('compute', 'image', 'import', 'from-object'):
                first_submitted.set()
            return result

        def upload(command, progress, multipart, stop):
            if self.cloud.uploads:
                if not first_submitted.wait(3):
                    raise AssertionError('First image must be submitted before remaining uploads finish')
            original(command, progress, multipart, stop)

        with patch.object(module, 'oci', side_effect=api), patch.object(module, 'upload_file', side_effect=upload):
            self.assertEqual(self.engine().run(), 0)
        self.assertTrue(self.cloud.active_during_upload)
        self.assertEqual(self.cloud.max_active, 1)

    def test_existing_bucket_and_unrelated_objects_are_preserved(self):
        self.cloud.exists = True
        self.cloud.objects['unrelated/file'] = b'do not delete'
        self.assertEqual(self.engine(1).run(), 0)
        self.assertFalse(self.cloud.deleted_bucket)
        self.assertEqual(self.cloud.objects, {'unrelated/file': b'do not delete'})

    def test_script_created_bucket_not_deleted_if_other_workdir_has_objects(self):
        original = self.cloud.upload

        def upload(*args):
            original(*args)
            self.cloud.objects['other-job/disk.qcow2'] = b'other job'

        with patch.object(module, 'upload_file', side_effect=upload):
            self.assertEqual(self.engine(1).run(), 0)
        self.assertFalse(self.cloud.deleted_bucket)
        self.assertIn('other-job/disk.qcow2', self.cloud.objects)

    def test_remote_state_resumes_uploaded_disk_without_download(self):
        engine = self.engine(1)
        engine.path.parent.mkdir(parents=True, exist_ok=True)
        engine.setup()
        engine.transfer(engine.state['images'][0])
        self.assertEqual(engine.state['images'][0]['status'], 'READY')
        engine.path.unlink()  # Simular perda do estado local; workdir deve bastar.
        downloads, uploads = len(self.cloud.downloads), len(self.cloud.uploads)
        resumed = self.engine(1)
        self.assertEqual(resumed.run(), 0)
        self.assertEqual(len(self.cloud.downloads), downloads)
        self.assertEqual(len(self.cloud.uploads), uploads)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_resumes_conversion_without_submitting_twice(self):
        engine = self.engine(1)
        engine.path.parent.mkdir(parents=True, exist_ok=True)
        engine.setup()
        engine.transfer(engine.state['images'][0])
        engine.submit(engine.state['images'][0])
        self.assertEqual(len(self.cloud.requests), 1)
        self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.requests), 1)

    def submitted_image(self):
        engine = self.engine(1)
        engine.path.parent.mkdir(parents=True, exist_ok=True)
        engine.setup()
        engine.transfer(engine.state['images'][0])
        engine.submit(engine.state['images'][0])
        return engine

    def forget_state(self, engine):
        engine.path.unlink()
        self.cloud.objects.pop(engine.workdir + 'state.json', None)

    def test_recovers_import_after_losing_local_and_remote_manifests(self):
        engine = self.submitted_image()
        self.forget_state(engine)
        self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.requests), 1)
        self.assertEqual(len(self.cloud.downloads), 1)
        self.assertEqual(len(self.cloud.uploads), 1)

    def test_recovers_import_when_batch_changes(self):
        self.submitted_image()
        self.args.results_file = str(self.root / 'changed-batch' / 'state.json')
        self.cloud.state_path = Path(self.args.results_file)
        self.assertEqual(self.engine(3).run(), 0)
        self.assertEqual(len(self.cloud.requests), 3)
        self.assertEqual(len(self.cloud.downloads), 3)

    def test_stale_ready_state_does_not_create_a_second_conversion(self):
        engine = self.submitted_image()
        stale = copy.deepcopy(engine.state)
        stale['images'][0].update(status='READY', image_id=None, work_request_id=None)
        module.atomic_json(engine.path, stale)
        self.cloud.objects[engine.workdir + 'state.json'] = json.dumps(stale).encode()
        self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_stale_state_recovers_conversion_that_already_completed(self):
        engine = self.submitted_image()
        self.cloud.requests['wr-1']['status'] = 'SUCCEEDED'
        engine.state['images'][0].update(status='READY', image_id=None, work_request_id=None)
        engine.save()
        self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_known_work_request_normalizes_stale_ready_status(self):
        engine = self.submitted_image()
        engine.state['images'][0]['status'] = 'READY'
        engine.save()
        self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_image_id_can_be_recovered_from_saved_work_request(self):
        engine = self.submitted_image()
        image_id = 'ocid1.image.oc1..recovered'
        self.cloud.requests['wr-1']['image_id'] = image_id
        engine.state['images'][0].update(image_id=None, status='UNKNOWN')
        engine.save()

        def api(*command):
            response = self.cloud.api(*command)
            if command[:3] == ('work-requests', 'work-request', 'get'):
                response['data']['resources'] = [{'identifier': image_id}]
            return response

        with patch.object(module, 'oci', side_effect=api):
            self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_duplicate_candidates_stop_without_creating_another_image(self):
        engine = self.submitted_image()
        self.cloud.requests['duplicate-work'] = dict(self.cloud.requests['wr-1'], image_id='duplicate-image')
        self.forget_state(engine)
        with self.assertRaisesRegex(RuntimeError, 'Mais de uma imagem candidata.*image-1.*duplicate-image'):
            self.engine(1).run()
        self.assertEqual(len(self.import_calls()), 1)
        self.assertEqual(len(self.cloud.downloads), 1)

    def test_same_name_import_without_matching_tag_blocks_new_submission(self):
        def api(*command):
            if command[:3] == ('compute', 'image', 'list'):
                return {'data': [{'id': 'unverified-image', 'lifecycle-state': 'IMPORTING'}]}
            return self.cloud.api(*command)

        with patch.object(module, 'oci', side_effect=api):
            with self.assertRaisesRegex(RuntimeError, 'não foi possível confirmar a mesma origem'):
                self.engine(1).run()
        self.assertFalse(self.import_calls())
        self.assertFalse(self.cloud.downloads)

    def test_missing_work_request_preserves_image_id_and_does_not_resubmit(self):
        engine = self.submitted_image()
        self.forget_state(engine)

        def api(*command):
            if command[:3] == ('work-requests', 'work-request', 'list'):
                return {'data': []}
            return self.cloud.api(*command)

        with patch.object(module, 'oci', side_effect=api):
            with self.assertRaisesRegex(RuntimeError, 'work request ausente ou ambíguo'):
                self.engine(1).run()
        state = json.loads(engine.path.read_text())
        self.assertEqual(state['images'][0]['image_id'], 'image-1')
        self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_submit_checks_cloud_again_after_initial_scan(self):
        engine = self.engine(1)
        engine.path.parent.mkdir(parents=True, exist_ok=True)
        engine.setup()
        engine.transfer(engine.state['images'][0])
        record = engine.state['images'][0]
        self.cloud.requests['existing-work'] = dict(status='IN_PROGRESS', polls=0, image_id='existing-image',
            object_name=record['object_name'], image_name=record['image_name'], compartment_id=record['compartment_id'],
            tags={'qcow2-import-key': engine.job + ':' + record['key']})
        engine.submit(record)
        self.assertEqual(record['work_request_id'], 'existing-work')
        self.assertEqual(record['status'], 'CONVERTING')
        self.assertFalse(self.import_calls())

    def test_download_metadata_is_removed_after_upload(self):
        engine = self.engine(1)
        engine.path.parent.mkdir(parents=True, exist_ok=True)
        engine.setup()
        record = engine.state['images'][0]
        engine.download(record)
        metadata = module.download_metadata_path(engine.local_file(record))
        metadata.write_text('{}')
        engine.upload(record)
        self.assertFalse(metadata.exists())
        self.assertFalse(engine.local_file(record).exists())

    def test_resumed_download_is_logged_uploaded_and_cleaned(self):
        engine = self.engine(1)
        record = engine.state['images'][0]
        disk = b'QFI\xfb\x00\x00\x00\x03fixture'

        def response(data, headers, status):
            stream = io.BytesIO(data)
            stream.headers = headers
            stream.status = status
            return stream

        headers = {'Content-Length': str(len(disk)), 'ETag': '"same-disk"'}
        with patch.object(module, 'urlopen', return_value=response(disk[:6], headers, 200)):
            with self.assertRaisesRegex(RuntimeError, 'Download incompleto'):
                REAL_DOWNLOAD(record['source'], engine.local_file(record), lambda value: None, threading.Event())
        headers = {'Content-Length': str(len(disk) - 6), 'Content-Range': f'bytes 6-{len(disk) - 1}/{len(disk)}',
                   'ETag': '"same-disk"'}
        with patch.object(module, 'download_file', side_effect=REAL_DOWNLOAD), \
             patch.object(module, 'urlopen', return_value=response(disk[6:], headers, 206)) as opened:
            self.assertEqual(engine.run(), 0)
        self.assertEqual(opened.call_args.args[0].get_header('Range'), 'bytes=6-')
        resumed = next(event for event in self.log_records()[0] if event['event'] == 'DOWNLOAD_RESUMED')
        self.assertEqual(resumed['offset_bytes'], 6)
        self.assertFalse(engine.local_file(record).exists())
        self.assertFalse(module.download_metadata_path(engine.local_file(record)).exists())

    def test_completed_execution_does_not_recreate_bucket_or_reimport(self):
        engine = self.engine(1)
        self.assertEqual(engine.run(), 0)
        # Estado concluído de uma versão anterior ainda deve ser reconhecido e limpo.
        module.atomic_json(engine.path, engine.state)
        calls = len(self.cloud.calls)
        self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.requests), 1)
        self.assertFalse(self.cloud.exists)
        self.assertEqual(self.cloud.calls[calls:], [('os', 'ns', 'get')])

    def test_capacity_rejection_keeps_staged_object_and_retries(self):
        self.cloud.reject_once = True
        self.assertEqual(self.engine(1).run(), 0)
        self.assertTrue(self.cloud.reject_seen)
        self.assertEqual(len(self.cloud.downloads), 1)
        self.assertEqual(len(self.cloud.uploads), 1)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_failed_conversion_is_reported_and_object_cleaned(self):
        self.cloud.fail_conversion = True
        engine = self.engine(1)
        self.assertEqual(engine.run(), 1)
        self.assertEqual(engine.state['images'][0]['status'], 'FAILED')
        self.assertEqual(engine.state['images'][0]['convert'], 'ERR')
        self.assertEqual(engine.state['images'][0]['cleanup'], 100)
        self.assertFalse(self.cloud.objects)
        self.assertNotIn('Imagens criadas:', self.out.getvalue())

    def test_lost_submission_response_recovers_without_resubmitting(self):
        self.cloud.unknown_once = True
        engine = self.engine(1)
        with self.assertRaises(module.CommandError):
            engine.run()
        self.assertEqual(len(self.cloud.requests), 1)
        self.assertEqual(engine.state['images'][0]['status'], 'UNKNOWN')
        self.assertFalse(self.cloud.deleted_bucket)
        self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_transfer_failure_preserves_workdir_for_resume(self):
        with patch.object(module, 'download_file', side_effect=RuntimeError('network failed')):
            engine = self.engine(1)
            self.assertEqual(engine.run(), 1)
        self.assertFalse(self.cloud.deleted_bucket)
        self.assertIn(engine.workdir + 'state.json', self.cloud.objects)
        self.assertEqual(self.engine(1).run(), 0)

    def test_table_width_and_stage_values(self):
        records = [dict(image_name='longest-image-name', download=100, upload=98, convert=None, cleanup=None),
                   dict(image_name='short', download=53, upload=None, convert=33, cleanup='ERR')]
        table = module.render_table(records)
        lines = table.splitlines()
        self.assertEqual(len({len(line) for line in lines}), 1)
        self.assertEqual(len(lines[0].split('+')[2]), len('longest-image-name') + 2)
        self.assertIn('Count', lines[1])
        self.assertIn('01/02', lines[3])
        self.assertIn('02/02', lines[4])
        self.assertIn('098%', table)
        self.assertIn('053%', table)
        self.assertIn('033%', table)
        self.assertIn('----', table)
        self.assertIn('ERR', table)

    def test_table_single_name_and_unicode_alignment(self):
        records = [dict(image_name='imagem-produção-日本', download=100)]
        lines = module.render_table(records).splitlines()
        self.assertEqual(len({module.display_width(line) for line in lines}), 1)
        self.assertIn('imagem-produção-日本', lines[3])

    def test_json_validated_before_cloud_access(self):
        batch = self.root / 'images.json'
        invalids = ['', '{}', '[]', 'null', '{"images": []}', '{"images": [42]}', '{"images": [',
                    module.EXAMPLE.replace('"Minha imagem 2"', 'null'),
                    module.EXAMPLE.replace('https://example.com/imagem-2.vmdk', 'file:///tmp/image')]
        for text in invalids:
            batch.write_text(text)
            with self.subTest(text=text), patch.object(module, 'oci') as api:
                self.assertEqual(module.main(['--batch-file', str(batch)]), 1)
                api.assert_not_called()

    def test_example_creation_never_overwrites_existing_file(self):
        example = self.root / 'example.json'
        self.assertEqual(module.main(['--create-example', str(example)]), 0)
        self.assertEqual(len(module.read_batch(example)), 2)
        self.assertEqual(module.main(['--create-example', str(example)]), 1)
        self.assertEqual(example.read_text(), module.EXAMPLE)

    def test_json_batch_accepts_utf8_bom_and_accents(self):
        batch = self.root / 'images.json'
        document = {'images': [dict(self.images[0], image_name='Imagem produção')]}
        batch.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8-sig')
        self.assertEqual(module.read_batch(batch), document['images'])

    def test_json_syntax_error_reports_line_and_column(self):
        batch = self.root / 'images.json'
        batch.write_text('{\n  "images": [\n}')
        with self.assertRaisesRegex(ValueError, 'JSON inválido na linha 3, coluna 1'):
            module.read_batch(batch)
        self.assertFalse(self.cloud.calls)

    def test_batch_rejects_yaml_comments_and_trailing_commas(self):
        batch = self.root / 'images.json'
        for text in ['images:\n  - url: https://example.com/image.qcow2',
                     '// comment\n' + module.EXAMPLE,
                     module.EXAMPLE.replace('"Minha imagem 2"', '"Minha imagem 2",')]:
            with self.subTest(text=text):
                batch.write_text(text)
                with self.assertRaisesRegex(ValueError, 'JSON inválido'):
                    module.read_batch(batch)
        self.assertFalse(self.cloud.calls)

    def test_mode_arguments(self):
        for arguments in [[], ['--source', 'https://example.com/image'],
                          ['--batch-file', 'images.json', '--compartment-id', 'ocid'],
                          ['--batch-file', 'images.json', '--source', 'url'],
                          ['--batch-file', 'images.json', '--max-in-progress', '0']]:
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as error:
                module.main(arguments)
            self.assertEqual(error.exception.code, 2)
        with patch.object(module.Importer, 'run', return_value=0) as run:
            self.assertEqual(module.main(['--source', 'https://example.com/disk.qcow2',
                                         '--compartment-id', 'ocid1.compartment.oc1..test', '--image-name', 'single']), 0)
            run.assert_called_once()

    def test_workdir_deterministic_even_if_json_order_changes(self):
        first = self.engine()
        second = module.Importer(list(reversed(copy.deepcopy(self.images))), self.args)
        self.assertEqual(first.workdir, second.workdir)
        self.assertEqual({row['object_name'] for row in first.state['images']},
                         {row['object_name'] for row in second.state['images']})

    def test_same_state_cannot_have_two_writers(self):
        with module.execution_lock(self.args.results_file):
            with self.assertRaisesRegex(RuntimeError, 'Outro processo'):
                with module.execution_lock(self.args.results_file):
                    self.fail('second writer allowed')

    def test_single_import_runs_through_conversion_and_cleanup(self):
        original = module.Importer
        created = []

        def create(images, args):
            args.results_file = str(self.cloud.state_path)
            args.poll_interval = 0.01
            engine = original(images, args)
            created.append(engine)
            return engine

        with patch.object(module, 'Importer', side_effect=create):
            self.assertEqual(module.main([
                '--source', self.images[0]['source'],
                '--compartment-id', self.images[0]['compartment_id'],
                '--image-name', self.images[0]['image_name'],
            ]), 0)
        self.assertEqual(len(self.cloud.requests), 1)
        state = created[0].state
        self.assertEqual(state['images'][0]['status'], 'DONE')
        self.assertTrue(state['bucket_deleted'])

    def test_interrupted_multipart_reuses_local_disk_and_upload_id(self):
        engine = self.engine(1)
        engine.path.parent.mkdir(parents=True, exist_ok=True)
        engine.setup()

        def interrupted(command, progress, multipart, stop):
            multipart('upload-to-resume')
            raise module.Stopped('interrupted')

        with patch.object(module, 'upload_file', side_effect=interrupted):
            with self.assertRaises(module.Stopped):
                engine.transfer(engine.state['images'][0])
        self.assertTrue(engine.local_file(engine.state['images'][0]).is_file())
        original = self.cloud.api

        def api(*command):
            if command[:3] == ('os', 'multipart', 'list'):
                return {'data': [{'object': engine.state['images'][0]['object_name'],
                                  'upload-id': 'upload-to-resume'}]}
            return original(*command)

        resumed = self.engine(1)
        resumed.path.parent.mkdir(parents=True, exist_ok=True)
        with patch.object(module, 'oci', side_effect=api):
            resumed.setup()

        def upload(command, *callbacks):
            self.assertEqual(command[3], 'resume-put')
            self.assertEqual(command[command.index('--upload-id') + 1], 'upload-to-resume')
            return self.cloud.upload(command, *callbacks)

        with patch.object(module, 'upload_file', side_effect=upload):
            resumed.transfer(resumed.state['images'][0])
        self.assertEqual(len(self.cloud.downloads), 1)
        self.assertEqual(resumed.state['images'][0]['status'], 'READY')
        self.assertFalse(resumed.local_file(resumed.state['images'][0]).exists())

    def test_cleanup_failure_is_retried_on_resume_without_reimport(self):
        engine = self.engine(1)
        original = self.cloud.api
        fail = True

        def api(*command):
            nonlocal fail
            if command[:3] == ('os', 'object', 'delete') and fail:
                fail = False
                raise self.cloud.error(503, 'Temporary storage failure', 'ServiceUnavailable')
            return original(*command)

        with patch.object(module, 'oci', side_effect=api):
            with self.assertRaises(module.CommandError):
                engine.run()
        self.assertEqual(engine.state['images'][0]['cleanup'], 'ERR')
        self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_status_command_reports_a_remote_failed_conversion(self):
        engine = self.engine(1)
        engine.path.parent.mkdir(parents=True, exist_ok=True)
        engine.setup()
        engine.transfer(engine.state['images'][0])
        engine.submit(engine.state['images'][0])
        self.cloud.fail_conversion = True
        self.cloud.requests['wr-1']['polls'] = 1
        self.assertEqual(module.main(['--check-status', str(engine.path)]), 1)
        self.assertIn('ERR', self.out.getvalue())

    def test_newer_local_checkpoint_wins_over_stale_cloud_manifest(self):
        engine = self.engine(1)
        engine.path.parent.mkdir(parents=True, exist_ok=True)
        engine.setup()
        engine.transfer(engine.state['images'][0])
        stale = self.cloud.objects[engine.workdir + 'state.json']
        engine.submit(engine.state['images'][0])
        self.cloud.objects[engine.workdir + 'state.json'] = stale
        resumed = self.engine(1)
        self.assertEqual(resumed.run(), 0)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_live_dashboard_does_not_wait_for_remote_manifest_upload(self):
        engine = self.engine(1)
        started = threading.Event()
        release = threading.Event()
        engine.remote_ready = True
        engine.state['namespace'] = 'namespace'

        def storage(*command):
            started.set()
            if not release.wait(3):
                raise AssertionError('Test did not release upload')
            return {}

        with patch.object(engine, 'storage', side_effect=storage):
            writer = threading.Thread(target=engine.save)
            writer.start()
            try:
                self.assertTrue(started.wait(3))
                self.assertTrue(engine.lock.acquire(timeout=0.2))
                engine.lock.release()
            finally:
                release.set()
                writer.join(3)

    def test_defaults_overrides_and_implicit_modes(self):
        batch = self.root / 'images.json'
        batch.write_text(module.EXAMPLE)
        for arguments, mode, maximum, workers, uploads in [
            (['--batch-file', str(batch)], 'batch', 3, 2, 2),
            (['--batch-file', str(batch), '--max-in-progress', '5', '--transfer-workers', '4',
              '--upload-workers', '3'], 'batch', 5, 4, 3),
            (['--source', self.images[0]['source'], '--compartment-id', self.images[0]['compartment_id'],
              '--image-name', 'single'], 'single', 3, 2, 2),
        ]:
            with self.subTest(arguments=arguments), patch.object(module, 'Importer') as importer:
                importer.return_value.run.return_value = 0
                self.assertEqual(module.main(arguments), 0)
                args = importer.call_args.args[1]
                self.assertEqual(args.mode, mode)
                self.assertEqual(args.max_in_progress, maximum)
                self.assertEqual(args.transfer_workers, workers)
                self.assertEqual(args.upload_workers, uploads)

    def test_upload_workers_must_be_positive(self):
        for value in ('0', '-1', 'invalid'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                module.main(['--batch-file', 'images.json', '--upload-workers', value])

    def test_removed_parameters_are_rejected(self):
        for option, value in [('--mode', 'batch'), ('--workdir', 'folder'),
                              ('--results-file', 'state.json'), ('--poll-interval', '1'),
                              ('--capacity-timeout', '60')]:
            with self.subTest(option=option), self.assertRaises(SystemExit) as error:
                module.main(['--batch-file', 'images.json', option, value])
            self.assertEqual(error.exception.code, 2)

    def test_count_handles_large_batches_and_keeps_input_order(self):
        records = [dict(image_name=f'image-{index}') for index in range(100)]
        lines = module.render_table(records).splitlines()
        self.assertEqual(len({len(line) for line in lines}), 1)
        self.assertIn('001/100', lines[3])
        self.assertIn('100/100', lines[-2])
        self.assertIn('image-0 ', lines[3])
        self.assertIn('image-99 ', lines[-2])

    def test_processed_counts_require_completion_or_recorded_failure(self):
        records = [dict(image_name=str(index), status=status) for index, status in enumerate(
            ['DONE', 'FAILED', 'TRANSFER_ERROR', 'CONVERTED', 'CONVERTING', 'PENDING'])]
        progress = module.render_progress(records)
        self.assertIn('Processados: 3/6 | Sucesso: 1 | Falhas: 2 | Pendentes: 3', progress)
        self.assertIn('01/06', progress)
        self.assertIn('06/06', progress)

    def test_single_count(self):
        self.assertIn('01/01', module.render_table([dict(image_name='single')]))

    def test_empty_multipart_cli_output_does_not_fail_startup_or_cleanup(self):
        original = self.cloud.api

        def api(*command):
            if command[:3] == ('os', 'multipart', 'list'):
                with patch.object(module, 'run_command', return_value=''):
                    return REAL_OCI(*command)
            return original(*command)

        with patch.object(module, 'oci', side_effect=api):
            self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.requests), 1)
        self.assertTrue(self.cloud.deleted_bucket)

    def test_two_transfer_workers_limit_simultaneous_downloads_to_two(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active = 0
        started = 0
        peak = 0

        def download(*arguments):
            nonlocal active, started, peak
            with lock:
                active += 1
                started += 1
                number = started
                peak = max(peak, active)
            try:
                if number <= 2:
                    barrier.wait(timeout=3)
                self.cloud.download(*arguments)
            finally:
                with lock:
                    active -= 1

        self.assertEqual(self.args.transfer_workers, 2)
        with patch.object(module, 'download_file', side_effect=download):
            self.assertEqual(self.engine().run(), 0)
        self.assertEqual(started, 3)
        self.assertEqual(peak, 2)

    def native_object_list_api(self, *command):
        result = self.cloud.api(*command)
        if command[:3] == ('os', 'object', 'list'):
            objects = result['data']['objects']
            native = {'data': objects, 'prefixes': []} if objects else {'prefixes': []}
            with patch.object(module, 'run_command', return_value=json.dumps(native)):
                return REAL_OCI(*command)
        return result

    def test_native_cli_empty_object_list_allows_startup_and_bucket_cleanup(self):
        with patch.object(module, 'oci', side_effect=self.native_object_list_api):
            self.assertEqual(self.engine(1).run(), 0)
        self.assertTrue(self.cloud.deleted_bucket)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_native_cli_object_list_preserves_bucket_with_unrelated_objects(self):
        self.cloud.objects['another-job/disk.qcow2'] = b'keep'
        with patch.object(module, 'oci', side_effect=self.native_object_list_api):
            self.assertEqual(self.engine(1).run(), 0)
        self.assertFalse(self.cloud.deleted_bucket)
        self.assertEqual(self.cloud.objects, {'another-job/disk.qcow2': b'keep'})

    def test_native_cli_object_list_resumes_staged_disk(self):
        engine = self.engine(1)
        engine.path.parent.mkdir(parents=True, exist_ok=True)
        engine.setup()
        engine.transfer(engine.state['images'][0])
        with patch.object(module, 'oci', side_effect=self.native_object_list_api):
            self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.downloads), 1)
        self.assertEqual(len(self.cloud.uploads), 1)
        self.assertEqual(len(self.cloud.requests), 1)

    def log_records(self):
        files = sorted(self.root.glob('import-qcow2-*.log'))
        self.assertTrue(files)
        return [[json.loads(line) for line in path.read_text().splitlines()] for path in files]

    def test_audit_log_records_image_steps_and_administration(self):
        self.assertEqual(self.engine(1).run(), 0)
        entries = self.log_records()[0]
        events = [entry['event'] for entry in entries]
        ordered = ['EXECUTION_STARTED', 'NAMESPACE_LOOKUP', 'NAMESPACE_RESOLVED',
                   'BUCKET_LOOKUP', 'BUCKET_CREATE_STARTED', 'BUCKET_CREATED',
                   'REMOTE_STATE_LOOKUP', 'REMOTE_STATE_NOT_FOUND', 'RESUME_SCAN',
                   'IMAGE_QUEUED', 'WORKDIR_READY', 'DOWNLOAD_STARTED', 'DOWNLOAD_COMPLETED',
                   'UPLOAD_STARTED', 'UPLOAD_COMPLETED', 'LOCAL_DISK_REMOVED',
                   'IMPORT_SUBMIT_STARTED', 'IMPORT_ACCEPTED', 'CONVERSION_COMPLETED',
                   'OBJECT_CLEANUP_STARTED', 'OBJECT_CLEANUP_COMPLETED',
                   'WORKDIR_CLEANUP_STARTED', 'WORKDIR_CLEANUP_COMPLETED',
                   'BUCKET_CONTENTS_CHECKED', 'BUCKET_DELETE_STARTED', 'BUCKET_DELETED',
                   'IMAGE_RESULT', 'EXECUTION_FINISHED']
        positions = [events.index(event) for event in ordered]
        self.assertEqual(positions, sorted(positions))
        accepted = next(entry for entry in entries if entry['event'] == 'IMPORT_ACCEPTED')
        self.assertEqual(accepted['work_request_id'], 'wr-1')
        self.assertEqual(accepted['image_id'], 'image-1')
        self.assertEqual(accepted['image_name'], self.images[0]['image_name'])
        self.assertEqual(entries[-1]['exit_code'], 0)
        self.assertEqual(entries[-1]['oci_calls']['compute image import from-object'], 1)
        self.assertTrue(all(entry['time'].endswith('+00:00') for entry in entries))
        self.assertTrue(self.cloud.deleted_bucket)
        self.assertTrue(list(self.root.glob('import-qcow2-*.log')))

    def test_audit_logs_all_images_without_repeating_percentage_updates(self):
        self.assertEqual(self.engine().run(), 0)
        entries = self.log_records()[0]
        for image in self.images:
            events = [entry for entry in entries if entry.get('image_name') == image['image_name']]
            self.assertEqual(sum(entry['event'] == 'DOWNLOAD_STARTED' for entry in events), 1)
            self.assertEqual(sum(entry['event'] == 'DOWNLOAD_COMPLETED' for entry in events), 1)
            self.assertEqual(sum(entry['event'] == 'IMAGE_RESULT' for entry in events), 1)
            states = [entry['status'] for entry in events if entry['event'] == 'CONVERSION_STATUS']
            self.assertEqual(len(states), len(set(states)))
        self.assertFalse(any('percent' in entry for entry in entries))

    def test_audit_keeps_separate_file_for_each_execution(self):
        self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(self.engine(1).run(), 0)
        logs = self.log_records()
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[1][0]['event'], 'EXECUTION_STARTED')
        self.assertIn('DOWNLOAD_STARTED', [entry['event'] for entry in logs[1]])
        self.assertEqual(len(self.cloud.requests), 2)

    def test_audit_records_fatal_setup_error(self):
        with patch.object(module, 'oci', side_effect=RuntimeError('namespace lookup failed')):
            with self.assertRaisesRegex(RuntimeError, 'namespace lookup failed'):
                self.engine(1).run()
        entries = self.log_records()[0]
        self.assertIn('EXECUTION_FAILED', [entry['event'] for entry in entries])
        self.assertEqual(entries[-1]['event'], 'EXECUTION_FINISHED')
        self.assertEqual(entries[-1]['exit_code'], 1)
        self.assertIn('namespace lookup failed', next(entry['error'] for entry in entries if entry['event'] == 'OCI_ERROR'))

    def test_audit_records_transfer_error_and_retained_workdir(self):
        with patch.object(module, 'download_file', side_effect=RuntimeError('download failed')):
            self.assertEqual(self.engine(1).run(), 1)
        entries = self.log_records()[0]
        events = [entry['event'] for entry in entries]
        self.assertIn('TRANSFER_FAILED', events)
        self.assertIn('WORKDIR_PRESERVED', events)
        self.assertEqual(entries[-1]['exit_code'], 1)

    def test_audit_records_retry_and_bucket_preservation(self):
        self.cloud.exists = True
        self.cloud.reject_once = True
        self.assertEqual(self.engine(1).run(), 0)
        entries = self.log_records()[0]
        events = [entry['event'] for entry in entries]
        self.assertIn('BUCKET_REUSED', events)
        self.assertIn('IMPORT_CAPACITY_REJECTED', events)
        self.assertIn('IMPORT_RETRY_SCHEDULED', events)
        preserved = next(entry for entry in entries if entry['event'] == 'BUCKET_PRESERVED')
        self.assertEqual(preserved['reason'], 'preexisting')

    def test_audit_records_keyboard_interrupt(self):
        engine = self.engine(1)
        with patch.object(engine, '_run', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.run()
        entries = self.log_records()[0]
        self.assertIn('EXECUTION_INTERRUPTED', [entry['event'] for entry in entries])
        self.assertEqual(entries[-1]['exit_code'], 130)

    def set_workdir_layout(self):
        self.args.results_file = str(self.root / '.oci-import' / 'job' / 'state.json')
        self.cloud.state_path = Path(self.args.results_file)

    def test_success_removes_local_workdir_and_keeps_only_log(self):
        self.set_workdir_layout()
        engine = self.engine(1)
        self.assertEqual(engine.run(), 0)
        self.assertFalse((self.root / '.oci-import').exists())
        remaining = list(self.root.iterdir())
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0].name.startswith('import-qcow2-'))
        self.assertEqual(remaining[0].suffix, '.log')
        events = [entry['event'] for entry in self.log_records()[0]]
        self.assertIn('LOCAL_STATE_REMOVED', events)
        self.assertIn('LOCAL_LOCK_REMOVED', events)
        self.assertIn('LOCAL_CLEANUP_COMPLETED', events)

    def test_cleanup_preserves_other_execution_workdir(self):
        self.set_workdir_layout()
        other = self.root / '.oci-import' / 'another-job' / 'state.json'
        other.parent.mkdir(parents=True)
        other.write_text('pending execution')
        self.assertEqual(self.engine(1).run(), 0)
        self.assertFalse(self.cloud.state_path.parent.exists())
        self.assertEqual(other.read_text(), 'pending execution')

    def test_failed_import_preserves_state_and_lock_for_resume(self):
        self.set_workdir_layout()
        self.cloud.fail_conversion = True
        self.assertEqual(self.engine(1).run(), 1)
        self.assertTrue(self.cloud.state_path.is_file())
        self.assertTrue(Path(str(self.cloud.state_path) + '.lock').is_file())
        self.assertTrue(list(self.root.glob('import-qcow2-*.log')))

    def test_old_logs_are_moved_before_workdir_removal(self):
        self.set_workdir_layout()
        legacy = self.cloud.state_path.parent / 'logs' / 'run-old.log'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('previous execution\n')
        self.assertEqual(self.engine(1).run(), 0)
        self.assertFalse((self.root / '.oci-import').exists())
        rescued = list(self.root.glob('run-old-*.log'))
        self.assertEqual(len(rescued), 1)
        self.assertEqual(rescued[0].read_text(), 'previous execution\n')

    def test_cleanup_does_not_delete_unrelated_files_in_workdir(self):
        self.set_workdir_layout()
        unrelated = self.cloud.state_path.parent / 'unrelated.txt'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text('keep')
        self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(unrelated.read_text(), 'keep')
        self.assertFalse(self.cloud.state_path.exists())
        self.assertFalse(Path(str(self.cloud.state_path) + '.lock').exists())

    def disk_file(self, name='local.vmdk', kind='VMDK'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'KDMV\x03\x00\x00\x00fixture' if kind == 'VMDK' else b'QFI\xfb\x00\x00\x00\x03fixture')
        return path

    def import_calls(self):
        return [call for call in self.cloud.calls if call[:4] == ('compute', 'image', 'import', 'from-object')]

    def test_url_vmdk_is_submitted_with_correct_type(self):
        self.images[0]['source'] = 'https://example.com/disk.vmdk'
        engine = self.engine(1)
        self.assertEqual(engine.run(), 0)
        command = self.import_calls()[0]
        self.assertEqual(command[command.index('--source-image-type') + 1], 'VMDK')
        self.assertEqual(engine.state['images'][0]['image_type'], 'VMDK')

    def test_local_disks_skip_download_and_preserve_originals(self):
        for kind in ('VMDK', 'QCOW2'):
            with self.subTest(kind=kind):
                disk = self.disk_file('local.' + kind.lower(), kind)
                original = disk.read_bytes()
                self.images[0]['source'] = str(disk)
                engine = self.engine(1)
                self.assertEqual(engine.run(), 0)
                command = self.import_calls()[-1]
                self.assertEqual(command[command.index('--source-image-type') + 1], kind)
                self.assertEqual(disk.read_bytes(), original)
                self.assertEqual(engine.state['images'][0]['download'], 'N/A')
                self.assertFalse(engine.path.exists())
        self.assertFalse(self.cloud.downloads)

    def test_mixed_batch_supports_local_and_remote_sources(self):
        disk = self.disk_file()
        self.images[1]['source'] = str(disk)
        self.images[2]['source'] = 'https://example.com/remote.vmdk'
        self.assertEqual(self.engine().run(), 0)
        self.assertEqual(len(self.cloud.downloads), 2)
        self.assertTrue(disk.is_file())
        kinds = [call[call.index('--source-image-type') + 1] for call in self.import_calls()]
        self.assertEqual(sorted(kinds), ['QCOW2', 'VMDK', 'VMDK'])

    def test_local_upload_and_conversion_do_not_wait_for_earlier_urls(self):
        disk = self.disk_file()
        self.images[2]['source'] = str(disk)
        self.args.transfer_workers = 1
        local_submitted = threading.Event()
        engine = self.engine()
        local_object = engine.state['images'][2]['object_name']

        def api(*command):
            response = self.cloud.api(*command)
            if command[:4] == ('compute', 'image', 'import', 'from-object'):
                if command[command.index('--name') + 1] == local_object:
                    local_submitted.set()
            return response

        def download(*arguments):
            if not local_submitted.wait(3):
                raise AssertionError('Local image must be submitted without waiting for URL downloads')
            self.cloud.download(*arguments)

        with patch.object(module, 'oci', side_effect=api), patch.object(module, 'download_file', side_effect=download):
            self.assertEqual(engine.run(), 0)
        self.assertTrue(local_submitted.is_set())
        self.assertEqual(self.cloud.uploads[0][0], str(disk))
        self.assertEqual(self.import_calls()[0][self.import_calls()[0].index('--name') + 1], local_object)
        self.assertEqual([row['image_name'] for row in engine.state['images']],
                         [row['image_name'] for row in self.images])
        self.assertTrue(disk.exists())

    def test_completed_download_is_uploaded_before_pending_downloads_on_resume(self):
        self.args.transfer_workers = 1
        self.args.upload_workers = 1
        engine = self.engine()
        completed = engine.state['images'][2]
        engine.path.parent.mkdir(parents=True, exist_ok=True)
        engine.local_file(completed).write_bytes(b'QFI\xfb\x00\x00\x00\x03fixture')
        completed['download'] = 100
        module.atomic_json(engine.path, engine.state)
        self.assertEqual(engine.run(), 0)
        self.assertEqual(self.cloud.uploads[0][1], completed['object_name'])
        self.assertEqual(len(self.cloud.downloads), 2)

    def test_two_downloads_and_two_uploads_can_run_simultaneously(self):
        local_one = dict(self.images[0], source=str(self.disk_file('first.vmdk')), image_name='local-one')
        local_two = dict(self.images[0], source=str(self.disk_file('second.vmdk')), image_name='local-two')
        self.images = self.images[:2] + [local_one, local_two]
        barrier = threading.Barrier(4)
        lock = threading.Lock()
        active = dict(download=0, upload=0)
        started = dict(download=0, upload=0)
        peaks = dict(download=0, upload=0)
        overlap = threading.Event()

        def perform(stage, function, arguments):
            with lock:
                active[stage] += 1
                started[stage] += 1
                number = started[stage]
                peaks[stage] = max(peaks[stage], active[stage])
                if active == dict(download=2, upload=2):
                    overlap.set()
            try:
                if number <= 2:
                    barrier.wait(timeout=5)
                function(*arguments)
            finally:
                with lock:
                    active[stage] -= 1

        with patch.object(module, 'download_file', side_effect=lambda *args: perform('download', self.cloud.download, args)), \
             patch.object(module, 'upload_file', side_effect=lambda *args: perform('upload', self.cloud.upload, args)):
            self.assertEqual(self.engine(4).run(), 0)
        self.assertTrue(overlap.is_set())
        self.assertEqual(peaks, dict(download=2, upload=2))
        self.assertEqual(started, dict(download=2, upload=4))

    def test_download_queue_continues_while_upload_is_busy(self):
        self.args.transfer_workers = 1
        self.args.upload_workers = 1
        last_download = threading.Event()
        downloads = 0

        def download(*arguments):
            nonlocal downloads
            self.cloud.download(*arguments)
            downloads += 1
            if downloads == 3:
                last_download.set()

        def upload(*arguments):
            if not last_download.wait(5):
                raise AssertionError('Uploads must not occupy download workers')
            self.cloud.upload(*arguments)

        with patch.object(module, 'download_file', side_effect=download), patch.object(module, 'upload_file', side_effect=upload):
            self.assertEqual(self.engine().run(), 0)
        self.assertEqual(downloads, 3)

    def test_format_detection_uses_header_instead_of_extension(self):
        disk = self.disk_file('misleading.qcow2', 'VMDK')
        self.images[0]['source'] = str(disk)
        self.assertEqual(self.engine(1).run(), 0)
        call = self.import_calls()[0]
        self.assertEqual(call[call.index('--source-image-type') + 1], 'VMDK')

    def test_json_local_paths_resolve_relative_to_input_file(self):
        disk = self.disk_file('inputs/disks/local.vmdk')
        batch = self.root / 'inputs' / 'images.json'
        batch.write_text(json.dumps({'images': [dict(self.images[0], source='disks/local.vmdk')]}))
        images = module.read_batch(batch)
        self.assertEqual(images[0]['source'], str(disk))
        self.assertFalse(self.cloud.calls)

    def test_missing_or_unsupported_local_input_fails_before_cloud_access(self):
        invalid = self.root / 'invalid.vmdk'
        invalid.write_bytes(b'not a supported disk')
        for path in (invalid, self.root / 'missing.qcow2', self.root):
            with self.subTest(path=path), patch.object(module, 'oci') as api:
                self.assertEqual(module.main(['--source', str(path), '--compartment-id', self.images[0]['compartment_id'],
                                              '--image-name', 'local']), 1)
                api.assert_not_called()
        self.assertTrue(invalid.exists())

    def test_unsupported_download_is_not_uploaded_or_imported(self):
        def invalid(url, filename, progress, stop):
            Path(filename).write_bytes(b'VDI or RAW')
            progress(100)

        with patch.object(module, 'download_file', side_effect=invalid):
            self.assertEqual(self.engine(1).run(), 1)
        self.assertFalse(self.cloud.uploads)
        self.assertFalse(self.import_calls())

    def test_local_source_is_preserved_on_conversion_failure(self):
        disk = self.disk_file()
        self.images[0]['source'] = str(disk)
        self.cloud.fail_conversion = True
        self.assertEqual(self.engine(1).run(), 1)
        self.assertTrue(disk.exists())
        self.assertFalse(self.cloud.downloads)

    def test_local_upload_failure_can_resume_without_deleting_source(self):
        disk = self.disk_file()
        self.images[0]['source'] = str(disk)
        with patch.object(module, 'upload_file', side_effect=RuntimeError('upload failed')):
            self.assertEqual(self.engine(1).run(), 1)
        self.assertTrue(disk.exists())
        self.assertEqual(self.engine(1).run(), 0)
        self.assertTrue(disk.exists())
        self.assertFalse(self.cloud.downloads)

    def test_resuming_staged_local_disk_preserves_original_and_skips_upload(self):
        disk = self.disk_file()
        self.images[0]['source'] = str(disk)
        engine = self.engine(1)
        engine.path.parent.mkdir(parents=True, exist_ok=True)
        engine.setup()
        engine.transfer(engine.state['images'][0])
        self.assertEqual(self.engine(1).run(), 0)
        self.assertEqual(len(self.cloud.uploads), 1)
        self.assertTrue(disk.exists())

    def test_modified_local_source_is_not_used_for_multipart_resume(self):
        disk = self.disk_file()
        self.images[0]['source'] = str(disk)
        with patch.object(module, 'upload_file', side_effect=RuntimeError('interrupted upload')):
            self.assertEqual(self.engine(1).run(), 1)
        disk.write_bytes(disk.read_bytes() + b'changed')
        self.assertEqual(self.engine(1).run(), 1)
        self.assertTrue(disk.exists())
        self.assertFalse(self.import_calls())

    def test_changing_source_during_upload_prevents_submission(self):
        disk = self.disk_file()
        self.images[0]['source'] = str(disk)

        def changed(*arguments):
            self.cloud.upload(*arguments)
            disk.write_bytes(disk.read_bytes() + b'changed')

        with patch.object(module, 'upload_file', side_effect=changed):
            self.assertEqual(self.engine(1).run(), 1)
        self.assertTrue(disk.exists())
        self.assertFalse(self.import_calls())
        self.assertFalse(any(name.endswith('.disk') for name in self.cloud.objects))

    def test_removed_qcow2_option_is_rejected(self):
        with self.assertRaises(SystemExit) as error:
            module.main(['--qcow2', 'https://example.com/image.qcow2'])
        self.assertEqual(error.exception.code, 2)

    def test_old_json_url_key_is_rejected(self):
        image = dict(self.images[0])
        image['url'] = image.pop('source')
        with self.assertRaisesRegex(ValueError, 'source'):
            module.validate_images([image])

    def test_single_local_source_cli_runs_without_downloading(self):
        disk = self.disk_file('local.vmdk')
        original = module.Importer

        def create(images, args):
            args.results_file = str(self.cloud.state_path)
            args.poll_interval = 0.01
            return original(images, args)

        with patch.object(module, 'Importer', side_effect=create):
            self.assertEqual(module.main(['--source', 'local.vmdk', '--compartment-id', self.images[0]['compartment_id'],
                                          '--image-name', 'local-vmdk']), 0)
        self.assertTrue(disk.exists())
        self.assertFalse(self.cloud.downloads)
        self.assertEqual(len(self.cloud.requests), 1)

    def test_local_source_supports_space_in_file_name(self):
        disk = self.disk_file('converted disks/my server.vmdk')
        images = module.validate_images([dict(self.images[0], source=str(disk))])
        self.assertEqual(images[0]['source'], str(disk))

    def test_same_disk_referenced_by_two_images_is_preserved(self):
        disk = self.disk_file()
        self.images[0]['source'] = str(disk)
        self.images[1]['source'] = str(disk)
        self.assertEqual(self.engine(2).run(), 0)
        self.assertEqual(len(self.cloud.requests), 2)
        self.assertTrue(disk.exists())
        self.assertFalse(self.cloud.downloads)


if __name__ == '__main__':
    unittest.main()
