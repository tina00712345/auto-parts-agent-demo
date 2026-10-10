#!/usr/bin/env python3
"""Add only missing photo-count fields. No record updates or AI generation."""
import argparse
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

API_BASE = 'https://open.feishu.cn/open-apis'
SCHEMA = [
    {'field_name': '封箱前货物照片', 'type': 17},
    {'field_name': '实物数量AI核对', 'type': 1},
    {'field_name': '可见对象及位置', 'type': 1},
    {'field_name': '可见完整商品数量', 'type': 2},
    {'field_name': '可见包装数量', 'type': 2},
    {'field_name': '实物总数量是否能确定', 'type': 3,
     'property': {'options': [{'name': s} for s in ['可确定', '无法确定', '待核对']]}},
    {'field_name': '图片中的SKU', 'type': 1},
    {'field_name': '实物与标签是否一致', 'type': 3,
     'property': {'options': [{'name': s} for s in ['一致', '不一致', '无法判断', '待核对']]}},
    {'field_name': '人工确认实物数量', 'type': 2},
    {'field_name': '实物核对状态', 'type': 3,
     'property': {'options': [{'name': s} for s in ['待核对', '已确认', '异常待处理']]}},
    {'field_name': '实物核对人', 'type': 1},
    {'field_name': '实物核对备注', 'type': 1},
]


class FeishuError(RuntimeError):
    pass


def request_json(method, path, token=None, payload=None):
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = None if payload is None else json.dumps(payload).encode('utf-8')
    request = Request(API_BASE + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            value = json.loads(response.read())
    except HTTPError as error:
        # Do not print authorization headers, secret payloads or full responses.
        raise FeishuError(f'HTTP {error.code}；检查应用权限及表格授权。') from None
    except (URLError, TimeoutError, OSError):
        raise FeishuError('连接失败或超时。创建请求可能已生效；不要盲目重试，重新运行会先读取现有列。') from None
    except (ValueError, TypeError):
        raise FeishuError('接口响应无法解析；停止操作。') from None
    if not isinstance(value, dict) or value.get('code') != 0:
        code = value.get('code', 'unknown') if isinstance(value, dict) else 'unknown'
        raise FeishuError(f'飞书接口错误 code={code}；检查官方文档、权限及目标表。')
    data = value.get('data', {})
    if not isinstance(data, dict):
        raise FeishuError('接口data格式异常；停止操作。')
    return data


def resolve_target(url, app_token, table_id):
    if url:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or not (host.endswith('.feishu.cn') or host.endswith('.larksuite.com')):
            raise FeishuError('请提供飞书多维表格的HTTPS链接。')
        match = re.fullmatch(r'/base/([^/]+)/?', parsed.path)
        if not match:
            raise FeishuError('当前只解析/base/多维表格链接；知识库/wiki/链接需先解析真实app_token。')
        if app_token and app_token != match.group(1):
            raise FeishuError('显式app_token与链接不一致；停止操作。')
        app_token = app_token or match.group(1)
        from_url = parse_qs(parsed.query).get('table', [None])[0]
        if table_id and from_url and table_id != from_url:
            raise FeishuError('显式table_id与链接中的table参数不一致；停止操作。')
        table_id = table_id or from_url
    if not app_token:
        raise FeishuError('缺少多维表格app_token或/base/链接。')
    for value in [app_token, table_id]:
        if value and not re.fullmatch(r'[A-Za-z0-9_-]+', value):
            raise FeishuError('目标标识格式不合法。')
    return app_token, table_id


def get_app_token():
    token = os.environ.get('FEISHU_TENANT_ACCESS_TOKEN')
    if token:
        return token
    app_id = os.environ.get('FEISHU_APP_ID'); secret = os.environ.get('FEISHU_APP_SECRET')
    if not app_id or not secret:
        raise FeishuError('未配置飞书授权。请在安全配置中设置FEISHU_APP_ID和FEISHU_APP_SECRET；不要发送到聊天或GitHub。')
    request = Request(API_BASE + '/auth/v3/tenant_access_token/internal',
                      data=json.dumps({'app_id': app_id, 'app_secret': secret}).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=20) as response:
            body = json.loads(response.read())
    except HTTPError as error:
        raise FeishuError(f'应用认证HTTP {error.code}。') from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise FeishuError('应用认证失败；检查凭据、应用类型或网络。') from None
    if not isinstance(body, dict) or body.get('code') != 0 or not isinstance(body.get('tenant_access_token'), str) or not body['tenant_access_token']:
        raise FeishuError('应用认证未返回有效令牌；停止操作。')
    return body['tenant_access_token']


def list_pages(path, token):
    items = []; cursor = None; seen = set()
    while True:
        query = {'page_size': 100}
        if cursor:
            query['page_token'] = cursor
        data = request_json('GET', path + '?' + urlencode(query), token)
        page_items = data.get('items')
        if not isinstance(page_items, list):
            raise FeishuError('列表响应不完整；停止操作，避免重复建列。')
        items.extend(page_items)
        if not data.get('has_more', False):
            return items
        cursor = data.get('page_token')
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise FeishuError('分页游标缺失或重复；停止操作。')
        seen.add(cursor)


def ensure_columns(app_token, table_id, token, expected_table_name='发货核对', apply=False):
    app_path = '/bitable/v1/apps/' + quote(app_token, safe='')
    tables = list_pages(app_path + '/tables', token)
    matches = [t for t in tables if (t.get('table_id') == table_id if table_id else t.get('name') == expected_table_name)]
    if len(matches) != 1:
        raise FeishuError('目标表不存在或不唯一；请核对链接和表名。')
    target = matches[0]
    if target.get('name') != expected_table_name:
        raise FeishuError('目标表名称与预期不一致；停止操作。')
    path = app_path + '/tables/' + quote(target['table_id'], safe='') + '/fields'
    existing = list_pages(path, token)
    by_name = {}
    for field in existing:
        by_name.setdefault(field['field_name'], []).append(field)
    missing = []; present = []
    for field in SCHEMA:
        matches = by_name.get(field['field_name'], [])
        if len(matches) > 1:
            raise FeishuError('存在重复同名列；请先整理，不自动选择。')
        if matches:
            old = matches[0]
            if old.get('type') != field['type']:
                raise FeishuError('同名列类型冲突：' + field['field_name'] + '；不覆盖原列。')
            required = {x['name'] for x in field.get('property', {}).get('options', [])}
            actual = {x.get('name') for x in (old.get('property') or {}).get('options', [])}
            if not required.issubset(actual):
                raise FeishuError('同名单选列缺少所需选项：' + field['field_name'] + '；不修改原列。')
            present.append(field['field_name'])
        else:
            missing.append(field)
    created = []
    if apply:
        for field in missing:
            data = request_json('POST', path, token, field)
            saved = data.get('field') or {}
            if not saved.get('field_id') or saved.get('field_name') != field['field_name'] or saved.get('type') != field['type']:
                raise FeishuError('创建响应与预期不一致；停止操作，重新读取后再处理。')
            created.append(field['field_name'])
    return {'模式': '新增缺少列' if apply else '只读检查', '目标表': target['name'],
            '已存在': present, '待新增': [f['field_name'] for f in missing], '本次已新增': created,
            '提示': '不会上传图片、修改记录或启动AI。实物数量AI核对先创建为文本列，豆包图片理解需在飞书配置。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', help='飞书/base/多维表格链接')
    parser.add_argument('--app-token'); parser.add_argument('--table-id')
    parser.add_argument('--table-name', default='发货核对')
    parser.add_argument('--apply', action='store_true', help='新增缺少列；不传则只读检查')
    parser.add_argument('--show-schema', action='store_true', help='显示字段清单，不连飞书')
    args = parser.parse_args()
    if args.show_schema:
        print(json.dumps(SCHEMA, ensure_ascii=False, indent=2)); return 0
    try:
        app_token, table_id = resolve_target(args.url, args.app_token, args.table_id)
        result = ensure_columns(app_token, table_id, get_app_token(), args.table_name, args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    except FeishuError as error:
        print(str(error), file=sys.stderr); return 1


if __name__ == '__main__':
    sys.exit(main())
