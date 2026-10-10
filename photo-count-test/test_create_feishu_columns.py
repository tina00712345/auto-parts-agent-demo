import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

spec = importlib.util.spec_from_file_location('columns', Path(__file__).with_name('create_feishu_columns.py'))
columns = importlib.util.module_from_spec(spec); spec.loader.exec_module(columns)


class FakeAPI:
    def __init__(self, fields=None, name='发货核对', fail_after=None):
        self.fields = fields or []; self.name = name; self.posts = []
        self.fail_after = fail_after

    def __call__(self, method, path, token=None, payload=None):
        clean = urlsplit(path)
        if clean.path.endswith('/tables'):
            return {'items': [{'table_id': 'tblDemo', 'name': self.name}], 'has_more': False}
        if method == 'GET':
            cursor = parse_qs(clean.query).get('page_token', [''])[0]
            if cursor:
                return {'items': self.fields[1:], 'has_more': False}
            return {'items': self.fields[:1], 'has_more': len(self.fields) > 1, 'page_token': 'second'}
        if self.fail_after is not None and len(self.posts) >= self.fail_after:
            raise columns.FeishuError('模拟超时')
        saved = json.loads(json.dumps(payload))
        saved['field_id'] = 'fld' + str(len(self.fields) + 1)
        self.fields.append(saved); self.posts.append(saved)
        return {'field': saved}


class ColumnTests(unittest.TestCase):
    def run_plan(self, api, apply=False):
        with patch.object(columns, 'request_json', side_effect=api):
            return columns.ensure_columns('appDemo', 'tblDemo', 'mock-token', apply=apply)

    def test_dry_run_never_posts(self):
        api = FakeAPI(); report = self.run_plan(api)
        self.assertEqual(len(report['待新增']), 12); self.assertEqual(api.posts, [])

    def test_create_then_replay_skip_all_across_pages(self):
        api = FakeAPI(); first = self.run_plan(api, True); second = self.run_plan(api, True)
        self.assertEqual(len(first['本次已新增']), 12)
        self.assertEqual(second['本次已新增'], [])
        self.assertEqual(len(api.posts), 12); self.assertEqual(len(second['已存在']), 12)

    def test_conflict_detected_before_any_write(self):
        field = dict(columns.SCHEMA[-1], type=2); api = FakeAPI([field])
        with self.assertRaises(columns.FeishuError): self.run_plan(api, True)
        self.assertEqual(api.posts, [])

    def test_missing_single_select_option_stops(self):
        field = dict(columns.SCHEMA[5], property={'options': [{'name': '待核对'}]})
        api = FakeAPI([field])
        with self.assertRaises(columns.FeishuError): self.run_plan(api, True)
        self.assertEqual(api.posts, [])

    def test_wrong_table_stops(self):
        api = FakeAPI(name='订单明细')
        with self.assertRaises(columns.FeishuError): self.run_plan(api, True)
        self.assertEqual(api.posts, [])

    def test_partial_failure_recovery_without_duplicate(self):
        api = FakeAPI(fail_after=1)
        with self.assertRaises(columns.FeishuError): self.run_plan(api, True)
        self.assertEqual(len(api.fields), 1)
        api.fail_after = None; self.run_plan(api, True)
        self.assertEqual(len(api.fields), 12)
        self.assertEqual(len({f['field_name'] for f in api.fields}), 12)

    def test_url_parsing_and_mismatch_guards(self):
        url = 'https://example.feishu.cn/base/appDemo?table=tblDemo'
        self.assertEqual(columns.resolve_target(url, None, None), ('appDemo', 'tblDemo'))
        for args in [(url, 'otherApp', None), (url, None, 'otherTable'),
                     ('https://example.feishu.cn/wiki/wikDemo', None, None)]:
            with self.assertRaises(columns.FeishuError): columns.resolve_target(*args)

    def test_repeated_pagination_cursor_stops(self):
        data = {'items': [], 'has_more': True, 'page_token': 'same'}
        with patch.object(columns, 'request_json', return_value=data):
            with self.assertRaises(columns.FeishuError): columns.list_pages('/test', 'mock-token')

    def test_auth_reads_top_level_token(self):
        body = io.BytesIO(json.dumps({'code': 0, 'tenant_access_token': 'mock-token'}).encode())
        with patch.dict(columns.os.environ, {'FEISHU_APP_ID': 'mock-id', 'FEISHU_APP_SECRET': 'mock-secret'}, clear=True), \
             patch.object(columns, 'urlopen', return_value=body):
            self.assertEqual(columns.get_app_token(), 'mock-token')


if __name__ == '__main__':
    unittest.main()
