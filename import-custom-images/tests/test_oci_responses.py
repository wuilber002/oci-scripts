import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'import-qcow2-disk-to-oci.py'
spec = importlib.util.spec_from_file_location('oci_responses', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OCIResponses(unittest.TestCase):
    def test_empty_multipart_list_has_data(self):
        for output in ['', '  \n', '{}', '[]', 'null', '{"data": null}', '{"data": []}']:
            with self.subTest(output=output), patch.object(module, 'run_command', return_value=output):
                response = module.oci('os', 'multipart', 'list', '--bucket-name', 'custom_image', '--all')
                self.assertEqual(response, {'data': []})

    def test_empty_object_list_has_objects(self):
        for output in ['', '{}', 'null', '{"data": null}']:
            with self.subTest(output=output), patch.object(module, 'run_command', return_value=output):
                self.assertEqual(module.oci('os', 'object', 'list'), {'data': {'objects': []}})

    def test_object_list_with_only_prefixes_is_empty(self):
        with patch.object(module, 'run_command', return_value='{"prefixes": []}'):
            result = module.oci('os', 'object', 'list', '--prefix', 'qcow2-import/job/', '--all')
        self.assertEqual(result['data']['objects'], [])
        self.assertEqual(result['prefixes'], [])

    def test_object_list_normalizes_native_and_nested_shapes(self):
        objects = [{'name': 'qcow2-import/job/disk.qcow2', 'size': 1024}]
        for payload in [
            {'data': objects, 'prefixes': []},
            {'data': {'objects': objects}},
            {'objects': objects},
            objects,
        ]:
            with self.subTest(payload=payload), patch.object(module, 'run_command', return_value=json.dumps(payload)):
                result = module.oci('os', 'object', 'list', '--all')
                self.assertEqual(result['data']['objects'], objects)

    def test_object_list_native_empty_shapes(self):
        for payload in [{'data': [], 'prefixes': []}, {'prefixes': [], 'next-start-with': None},
                        {'data': {'objects': None}}, []]:
            with self.subTest(payload=payload), patch.object(module, 'run_command', return_value=json.dumps(payload)):
                self.assertEqual(module.oci('os', 'object', 'list')['data']['objects'], [])

    def test_invalid_object_list_is_not_treated_as_empty_bucket(self):
        for payload in [{'unexpected': []}, {'data': {'unexpected': []}},
                        {'data': 'invalid'}, {'data': [{}]}]:
            with self.subTest(payload=payload), patch.object(module, 'run_command', return_value=json.dumps(payload)):
                with self.assertRaisesRegex(RuntimeError, 'oci os object list'):
                    module.oci('os', 'object', 'list')

    def test_missing_data_on_namespace_is_explained(self):
        with patch.object(module, 'run_command', return_value='{}'):
            with self.assertRaisesRegex(RuntimeError, 'oci os ns get:.*data'):
                module.oci('os', 'ns', 'get')

    def test_missing_data_on_bucket_is_explained(self):
        with patch.object(module, 'run_command', return_value='{}'):
            with self.assertRaisesRegex(RuntimeError, 'oci os bucket get:.*data'):
                module.oci('os', 'bucket', 'get')

    def test_invalid_json_is_explained(self):
        with patch.object(module, 'run_command', return_value='unexpected CLI output'):
            with self.assertRaisesRegex(RuntimeError, 'oci os ns get:.*JSON'):
                module.oci('os', 'ns', 'get')

    def test_expected_data_and_work_request_headers_are_preserved(self):
        output = '{"data": {"id": "image-1"}, "opc-work-request-id": "wr-1"}'
        with patch.object(module, 'run_command', return_value=output):
            result = module.oci('compute', 'image', 'import', 'from-object')
        self.assertEqual(result['data']['id'], 'image-1')
        self.assertEqual(result['opc-work-request-id'], 'wr-1')

    def test_empty_delete_and_file_download_remain_valid(self):
        for command in [('os', 'object', 'delete'), ('os', 'bucket', 'delete'), ('os', 'object', 'get')]:
            with self.subTest(command=command), patch.object(module, 'run_command', return_value=''):
                self.assertEqual(module.oci(*command), {})


if __name__ == '__main__':
    unittest.main()
