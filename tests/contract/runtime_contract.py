#!/usr/bin/env python3

import argparse
import json
import urllib.error
import urllib.request


def request(base_url, path, method='GET', payload=None, token=None):
    body = None
    headers = {'Accept': 'application/json'}
    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(
        f'{base_url.rstrip("/")}{path}',
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            content_type = response.headers.get_content_type()
            content = response.read()
            if content_type == 'application/json':
                return response.status, json.loads(content.decode('utf-8'))
            return response.status, content
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', errors='replace')
        raise AssertionError(
            f'{method} {path} returned HTTP {error.code}: {detail[:500]}'
        ) from error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--username', required=True)
    parser.add_argument('--password', required=True)
    args = parser.parse_args()

    status, _ = request(args.base_url, '/health')
    if status != 200:
        raise AssertionError('Superset health endpoint did not return 200')

    _, login = request(
        args.base_url,
        '/api/v1/security/login',
        method='POST',
        payload={
            'username': args.username,
            'password': args.password,
            'provider': 'db',
            'refresh': True,
        },
    )
    token = login.get('access_token')
    if not token:
        raise AssertionError('security-login contract did not return an access token')

    collection_query = '?q=%28page%3A0%2Cpage_size%3A1%29'
    for endpoint in ('dashboard', 'chart', 'dataset'):
        status, result = request(
            args.base_url,
            f'/api/v1/{endpoint}/{collection_query}',
            token=token,
        )
        if status != 200 or 'result' not in result:
            raise AssertionError(f'{endpoint} collection contract failed')

    _, guest = request(
        args.base_url,
        '/api/v1/security/guest_token/',
        method='POST',
        token=token,
        payload={
            'resources': [],
            'rls': [],
            'user': {
                'username': 'xpbuilder-contract',
                'first_name': 'XPBuilder',
                'last_name': 'Contract',
            },
        },
    )
    if not guest.get('token'):
        raise AssertionError('guest-token contract did not return a token')

    logo_status, logo = request(
        args.base_url,
        '/static/assets/images/advance-bi-logo.png',
    )
    if logo_status != 200 or not isinstance(logo, bytes) or not logo.startswith(b'\x89PNG'):
        raise AssertionError('XPBuilder branding asset is unavailable')

    print('XPBuilder runtime API contract passed')


if __name__ == '__main__':
    main()
